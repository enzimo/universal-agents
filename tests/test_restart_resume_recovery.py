"""A task interrupted by the restart it asked for is told what happened.

TeamArchitect's own task always ends when a manager restart it asked for is
sent, and its conversation is not kept. Restart recovery reads the restart
receipts that name the task (``runtime_restart_service``) and records a
directive the next turn reads, so the task does not ask for the same restart
again. Real TaskManager and SQLite; receipts as ``RuntimeRestartService.apply``
writes them (that writer is covered in ``test_runtime_restart_service``).
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
import logging
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_agents.runtime_recovery import recover_runtime_after_restart
from custom_agents.access_request_runtime import _resume_without_live_turn
from custom_agents.task_human_interaction_blockers import ACCESS_REQUEST_BLOCKER, record_human_interaction_blocker
from custom_agents.task_manager import TaskManager
from custom_agents.task_runtime_state import TurnStartNotes
from custom_agents.task_steering_runtime import handle_task_steering_message
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task, TaskSteering
from custom_messages.enums import MessageMetaType, ProgressState, TaskSteeringStatus, TaskSteeringType
from storage.config import resolve_storage_settings
from custom_messages.contracts.access_v2 import AccessRequestStatus

TASK_ID = "task_approve_restart"


def _manager(root: Path, agent_type: str) -> TaskManager:
    settings = resolve_storage_settings(
        {"backend": "sqlite", "sqlite": {"path": "runtime/task_history.db"}},
        base_dir=str(root),
    )
    manager = TaskManager(storage_settings=settings, logger=logging.getLogger("test.restart_resume"))
    manager.set_owner_identity("manager_1", "Manager", "team_1", agent_type)
    return manager


def _receipt(root: Path, change_id: str, *, task_id: str | None, scope: str = "manager", note: str = "") -> None:
    receipts = root / "runtime" / "restart-authorization" / "receipts"
    receipts.mkdir(parents=True, exist_ok=True)
    payload = {
        "change_id": change_id,
        "dispatched_at_utc": f"2026-10-11T20:{39 + len(list(receipts.iterdir())):02d}:40+00:00",
        "scope": scope,
        "reason": "Load the new context window",
        "authority": {
            "approval": {
                "mode": "person_approval",
                "approved_by": "user_owner",
                "approved_at_utc": "2026-10-11T20:39:30+00:00",
            }
        },
        "approval_note": note,
    }
    if task_id is not None:
        payload["source_task_id"] = task_id
    (receipts / f"{change_id}.json").write_text(json.dumps(payload), encoding="utf-8")


def _interrupted_task(root: Path, agent_type: str) -> None:
    """The task was running when the manager restarted (its last saved state)."""

    manager = _manager(root, agent_type)
    task = Task(task_id=TASK_ID, task_type=MessageMetaType.REQUEST, problem_statement="1")
    assert manager.add_task(task, MessageHeader(from_agent_id="gateway"), MessageMeta(), owner_person_id=None)
    assert manager.get_next_task_to_process() == TASK_ID
    manager.update_main_task_state(TASK_ID, ProgressState.IN_PROGRESS, datetime.now(timezone.utc).isoformat())
    manager.append_runtime_resume_directive(TASK_ID, "A directive saved before the restart.")


def _restarted_agent(root: Path, agent_type: str = "TeamArchitect") -> SimpleNamespace:
    return SimpleNamespace(
        deferred_inbox=SimpleNamespace(
            stored_team_id=AsyncMock(return_value=None),
            restore_role=AsyncMock(return_value=False),
            restore=AsyncMock(return_value=False),
        ),
        id="manager_1",
        team_id="team_1",
        named_agent_type=agent_type,
        same_role_runtime_takeover_enabled=True,
        task_manager=_manager(root, agent_type),
        instance_root=str(root),
        _agent_state_store=None,
        _logger=logging.getLogger("test.restart_resume"),
        _role_takeover_liveness_grace_seconds=lambda: 60.0,
        _recovery_retry_deadline_seconds=lambda: 900.0,
        _parse_iso_timestamp=lambda value: datetime.fromisoformat(value) if value else None,
    )


def _history(manager: TaskManager) -> list[str]:
    return [entry.summary for entry in manager.get_task_history_entries(TASK_ID, limit=None)]


@pytest.mark.asyncio
async def test_the_resumed_task_learns_its_restarts_happened(tmp_path: Path, caplog) -> None:
    _interrupted_task(tmp_path, "TeamArchitect")
    _receipt(tmp_path, "runtime_restart_aaaaaaaaaaaa", task_id=TASK_ID, note="Go ahead")
    _receipt(tmp_path, "runtime_restart_bbbbbbbbbbbb", task_id=TASK_ID, scope="all")
    _receipt(tmp_path, "runtime_restart_cccccccccccc", task_id=TASK_ID, scope="gateway")  # did not end the task
    _receipt(tmp_path, "runtime_restart_dddddddddddd", task_id="task_other")
    _receipt(tmp_path, "runtime_restart_eeeeeeeeeeee", task_id=None)  # written before receipts named tasks
    agent = _restarted_agent(tmp_path)

    with caplog.at_level(logging.INFO):
        await recover_runtime_after_restart(agent)

    manager = agent.task_manager
    assert manager.get_task_info(TASK_ID).state == ProgressState.ACCEPTED
    assert manager.has_queued_top_level_tasks()
    directive = manager.read_runtime_resume_directive(TASK_ID)
    assert directive is not None
    assert directive.startswith("A directive saved before the restart.")
    assert "runtime_restart_aaaaaaaaaaaa" in directive and "runtime_restart_bbbbbbbbbbbb" in directive
    assert directive.index("runtime_restart_aaaaaaaaaaaa") < directive.index("runtime_restart_bbbbbbbbbbbb")
    for other in ("cccccccccccc", "dddddddddddd", "eeeeeeeeeeee"):
        assert other not in directive
    assert 'The approver wrote: "Go ahead"' in directive
    assert "restarted the manager 2 times" in directive
    assert any("runtime_restart_aaaaaaaaaaaa" in line for line in _history(manager))
    assert "event=restart_outcome_recorded" in caplog.text


@pytest.mark.asyncio
async def test_a_second_restart_before_the_turn_does_not_repeat_the_directive(tmp_path: Path) -> None:
    _interrupted_task(tmp_path, "TeamArchitect")
    _receipt(tmp_path, "runtime_restart_aaaaaaaaaaaa", task_id=TASK_ID)
    first = _restarted_agent(tmp_path)
    await recover_runtime_after_restart(first)
    # The manager stops again before the re-queued task runs.
    first.task_manager.update_main_task_state(TASK_ID, ProgressState.IN_PROGRESS, datetime.now(timezone.utc).isoformat())
    second = _restarted_agent(tmp_path)

    await recover_runtime_after_restart(second)

    directive = second.task_manager.read_runtime_resume_directive(TASK_ID)
    assert directive is not None and directive.count("runtime_restart_aaaaaaaaaaaa") == 1


@pytest.mark.asyncio
async def test_a_task_that_asked_for_no_restart_gets_no_restart_directive(tmp_path: Path) -> None:
    _interrupted_task(tmp_path, "TeamArchitect")
    _receipt(tmp_path, "runtime_restart_dddddddddddd", task_id="task_other")
    agent = _restarted_agent(tmp_path)

    await recover_runtime_after_restart(agent)

    assert agent.task_manager.read_runtime_resume_directive(TASK_ID) == "A directive saved before the restart."


@pytest.mark.asyncio
async def test_other_agent_types_do_not_read_restart_receipts(tmp_path: Path) -> None:
    _interrupted_task(tmp_path, "TeamLead")
    _receipt(tmp_path, "runtime_restart_aaaaaaaaaaaa", task_id=TASK_ID)
    agent = _restarted_agent(tmp_path, "TeamLead")

    await recover_runtime_after_restart(agent)

    assert agent.task_manager.read_runtime_resume_directive(TASK_ID) == "A directive saved before the restart."


@pytest.mark.asyncio
@pytest.mark.parametrize("saved_state", [ProgressState.WAITING, ProgressState.ACCEPTED, ProgressState.IN_PROGRESS])
async def test_restart_outcome_survives_another_access_wait(tmp_path: Path, saved_state: ProgressState) -> None:
    _interrupted_task(tmp_path, "TeamArchitect")
    original = _manager(tmp_path, "TeamArchitect")
    original.restore_runtime_state()
    # The next tool can persist its blocker and its state on either side of
    # the supervisor stopping the manager. Both snapshots must recover.
    record_human_interaction_blocker(original, TASK_ID, {
        "kind": ACCESS_REQUEST_BLOCKER, "access_request_id": "another_request",
    })
    original.update_main_task_state(TASK_ID, saved_state, datetime.now(timezone.utc).isoformat())
    _receipt(tmp_path, "runtime_restart_aaaaaaaaaaaa", task_id=TASK_ID, note="Check the loaded settings")
    agent = _restarted_agent(tmp_path)
    agent.paused = True

    await recover_runtime_after_restart(agent)
    pending = agent.task_manager.task_state.runtime_resume_directives[TASK_ID]
    assert "runtime_restart_aaaaaaaaaaaa" in pending
    assert "Check the loaded settings" in pending
    # Another approval adds its own continuation; it cannot replace the
    # restart result before the next model invocation consumes it.
    decision = SimpleNamespace(task_id=TASK_ID, request_id="another_request",
                               status=AccessRequestStatus.GRANTED, decision_note="Continue")
    await _resume_without_live_turn(agent, decision)
    directive = agent.task_manager.read_runtime_resume_directive(TASK_ID)
    assert directive is not None
    assert "runtime_restart_aaaaaaaaaaaa" in directive
    assert "Check the loaded settings" in directive
    assert "another_request" in directive
    assert agent.task_manager.get_task_info(TASK_ID).state == ProgressState.ACCEPTED


def test_notes_for_the_next_turn_are_added_never_replaced(tmp_path: Path) -> None:
    """Restart recovery's note survives whatever else tells the task something before its turn.

    For example, a final answer saved before the restart that fails afterwards
    leaves a recovery brief; the turn must still learn the restart is done.
    """

    manager = _manager(tmp_path, "TeamArchitect")
    restart_note = "The manager restarted for the restart this task asked for. It is done."
    brief = "Recovery brief: the saved final answer could not be delivered; check it and answer again."
    manager.append_runtime_resume_directive(TASK_ID, restart_note)
    manager.append_runtime_resume_directive(TASK_ID, brief)
    manager.append_runtime_resume_directive(TASK_ID, restart_note)

    note = f"{restart_note}\n\n{brief}"
    assert manager.read_runtime_resume_directive(TASK_ID) == note
    manager.release_turn_start_notes(TurnStartNotes(TASK_ID, resume_directive=note))  # its decision was recorded
    assert manager.read_runtime_resume_directive(TASK_ID) is None
    assert not hasattr(manager, "record_runtime_resume_directive"), "Notes have one way in, which adds"


def test_only_the_task_manager_writes_notes_for_the_next_turn() -> None:
    """Every other module leaves a note through ``append_runtime_resume_directive``."""

    root = Path(__file__).resolve().parents[1] / "custom_agents"
    allowed = {"task_manager.py", "task_runtime_state.py", "runtime_snapshot_codec.py"}
    writers = sorted(
        path.name
        for path in root.rglob("*.py")
        if path.name not in allowed and "runtime_resume_directives" in path.read_text(encoding="utf-8")
    )
    assert writers == []


@pytest.mark.parametrize("first_response", [
    "Only send it to Alice and Bob.",
    "Only send it to Alice\n\nInclude Bob as well.",
])
@pytest.mark.asyncio
async def test_distinct_requester_corrections_survive_until_the_restarted_tasks_next_turn(
    tmp_path: Path, first_response: str
) -> None:
    """Accept both replies before running, including a shorter correction."""

    _interrupted_task(tmp_path, "TeamArchitect")
    agent = _restarted_agent(tmp_path)
    agent.paused = True
    agent._interrupted_turn_sessions = {}
    agent._processed_task_steering_ids = set()
    agent._agent_id_by_name = {}
    agent.named_agent_type_queue_map = {}
    agent.name = "Manager"
    agent.team_comms = SimpleNamespace(send_message=AsyncMock())
    await recover_runtime_after_restart(agent)
    second_response = "Only send it to Alice"

    replies = [
        ("steer_first", first_response),
        ("steer_second", second_response),
        ("steer_second", second_response),  # Redelivery must not add another note.
    ]
    for steering_id, response in replies:
        await handle_task_steering_message(
            agent,
            TaskSteering(
                steering_id=steering_id,
                steering_type=TaskSteeringType.INTERRUPT_RESPONSE,
                target_task_id=TASK_ID,
                guidance_text=response,
            ),
            MessageHeader(from_agent_id="gateway", response_routing_key="gateway_response"),
            MessageMeta(is_response_requested=True),
        )

    statuses = [call.kwargs["message"].msg_content for call in agent.team_comms.send_message.await_args_list]
    assert [(status.steering_id, status.status) for status in statuses] == [
        ("steer_first", TaskSteeringStatus.ACCEPTED),
        ("steer_second", TaskSteeringStatus.ACCEPTED),
        ("steer_second", TaskSteeringStatus.ACCEPTED),
    ]
    # Read the persisted notes through a new manager, as another restart would.
    restored = _manager(tmp_path, "TeamArchitect")
    restored.restore_runtime_state()
    directive = restored.read_runtime_resume_directive(TASK_ID)
    assert directive is not None
    assert "A directive saved before the restart." in directive
    assert directive.count("Runtime mid-turn interrupt response") == 2
    assert first_response in directive
    assert directive.endswith(second_response)
    restored.release_turn_start_notes(TurnStartNotes(TASK_ID, resume_directive=directive))
    assert restored.read_runtime_resume_directive(TASK_ID) is None


def test_resume_note_deduplication_matches_whole_blocks(tmp_path: Path) -> None:
    """Overlapping text stays distinct, while a repeated multi-paragraph note does not grow."""

    manager = _manager(tmp_path, "TeamArchitect")
    notes = [
        "First paragraph.\n\nSecond paragraph.",
        "Only send it to Alice and Bob.",
        "Only send it to Alice",
        "Bob.",
    ]
    for note in notes + notes:
        manager.append_runtime_resume_directive(TASK_ID, note)

    assert manager.read_runtime_resume_directive(TASK_ID) == "\n\n".join(notes)


def _turn_with_saved_notes(manager: TaskManager, invoke):
    """The real turn owner reading notes in ``manager``, and a model result.

    Its outbox records the decision as the real one does, as a batch in
    ``manager`` that releases what the turn read (``TurnStartNotes``).
    """
    import dataclasses

    from tests.test_runtime_owner_boundaries import build_turn_owner

    executor, result = build_turn_owner(
        invoke=invoke,
        guidance=deque(["", ""]),
        committed=[],
        cleared=[],
        task_operations={
            "read_runtime_resume_directive": manager.read_runtime_resume_directive,
            "read_partial_tool_call_limit": manager.read_partial_tool_call_limit,
            "release_turn_start_notes": manager.release_turn_start_notes,
        },
    )

    async def record_decision(*, incoming_content, start_notes=None, **_kwargs):
        assert manager.create_turn_action_batch(incoming_content.task_id, [], release_notes=start_notes)
        return Task(task_id=incoming_content.task_id, task_type=MessageMetaType.RESPONSE, answer="done")

    executor.effects = dataclasses.replace(executor.effects, execute_turn_decision_outbox=record_decision)
    return executor, result


DECISION_NOTE = "The access request this task paused for was granted. Continue the task."


@pytest.mark.asyncio
async def test_a_turn_cancelled_at_shutdown_keeps_its_note_through_a_restart(tmp_path: Path) -> None:
    """2026-10-17: the note was removed at turn start, so a turn cancelled at
    shutdown came back after the restart without the access decision it carried."""

    _interrupted_task(tmp_path, "WebResearchAgent")
    manager = _manager(tmp_path, "WebResearchAgent")
    manager.restore_runtime_state()
    manager.append_runtime_resume_directive(TASK_ID, DECISION_NOTE)
    manager.record_partial_tool_call_limit(TASK_ID, 3)
    started, prompts = asyncio.Event(), []

    async def model_call(prompt, **_kwargs):
        prompts.append(prompt)
        started.set()
        await asyncio.Event().wait()

    executor, _ = _turn_with_saved_notes(manager, model_call)
    turn = asyncio.create_task(
        executor.process_llm_interaction(
            Task(task_id=TASK_ID, task_type=MessageMetaType.REQUEST, problem_statement="1"),
            MessageHeader(),
            MessageMeta(),
        )
    )
    await asyncio.wait_for(started.wait(), 5)
    assert DECISION_NOTE in prompts[0]  # the turn read the note
    turn.cancel()  # shutdown cancels the turn
    with pytest.raises(asyncio.CancelledError):
        await turn

    agent = _restarted_agent(tmp_path, "WebResearchAgent")  # restarted within the grace period
    agent._finalize_conversation_turn = AsyncMock()
    agent._send_task_error_response = AsyncMock()
    await recover_runtime_after_restart(agent)

    restarted = agent.task_manager
    assert restarted.get_task_info(TASK_ID).state == ProgressState.ACCEPTED
    assert restarted.has_queued_top_level_tasks()
    note = restarted.read_runtime_resume_directive(TASK_ID)
    assert note is not None and DECISION_NOTE in note
    assert restarted.read_partial_tool_call_limit(TASK_ID) == 3
    agent._send_task_error_response.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_recorded_decision_removes_only_the_note_its_turn_read(tmp_path: Path) -> None:
    """A note added while the turn runs is for the task's next turn."""

    _interrupted_task(tmp_path, "WebResearchAgent")
    manager = _manager(tmp_path, "WebResearchAgent")
    manager.restore_runtime_state()
    manager.record_partial_tool_call_limit(TASK_ID, 3)
    later_note = "A failed action's recovery brief, added during the turn."

    async def model_call(_prompt, **_kwargs):
        manager.append_runtime_resume_directive(TASK_ID, later_note)
        return result

    executor, result = _turn_with_saved_notes(manager, model_call)
    response = await executor.process_llm_interaction(
        Task(task_id=TASK_ID, task_type=MessageMetaType.REQUEST, problem_statement="1"),
        MessageHeader(),
        MessageMeta(),
    )
    assert response.task_id == TASK_ID
    assert manager.read_runtime_resume_directive(TASK_ID) == later_note
    assert manager.read_partial_tool_call_limit(TASK_ID) is None
    reloaded = _manager(tmp_path, "WebResearchAgent")
    reloaded.restore_runtime_state()
    assert reloaded.read_runtime_resume_directive(TASK_ID) == later_note  # saved, not only in memory


@pytest.mark.asyncio
async def test_a_turn_that_raises_keeps_its_note_for_the_next_attempt(tmp_path: Path) -> None:
    """A provider failover or retry queues the task again; that turn needs the note too."""

    _interrupted_task(tmp_path, "WebResearchAgent")
    manager = _manager(tmp_path, "WebResearchAgent")
    manager.restore_runtime_state()
    manager.append_runtime_resume_directive(TASK_ID, DECISION_NOTE)

    async def failing_model_call(_prompt, **_kwargs):
        raise ConnectionError("the provider is unavailable")

    executor, _ = _turn_with_saved_notes(manager, failing_model_call)
    with pytest.raises(Exception):
        await executor.process_llm_interaction(
            Task(task_id=TASK_ID, task_type=MessageMetaType.REQUEST, problem_statement="1"),
            MessageHeader(),
            MessageMeta(),
        )
    note = manager.read_runtime_resume_directive(TASK_ID)
    assert note is not None and DECISION_NOTE in note
