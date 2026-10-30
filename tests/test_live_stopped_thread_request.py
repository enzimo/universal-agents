"""Stopping a thread's request ends it, so the thread takes new requests again.

A thread runs one request at a time, and the gateway records which one runs
(``assistant_gateway.domain.request_state``). These tests run the real gateway
and a real team lead over a live NATS server (the harness of
``tests.test_live_busy_thread_guidance``), stop the running request with the
gateway's stop control (what ``/stop`` and Web Chat's Tasks panel send), and
check that the thread is free again.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from assistant_gateway.application.operational_control import GatewayStopControlRequest
from tests.broker_access_support import forget_task_context_keys, use_gateway_key
from tests.test_live_busy_thread_guidance import (
    WAIT_SECONDS,
    _answer,
    _settle,
    _team,
    _Turn,
    _wait_until,
)


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.integration,
    pytest.mark.live,
    pytest.mark.nats,
]


@pytest.fixture(autouse=True)
def _gateway_signs_people(monkeypatch: pytest.MonkeyPatch):
    """The lead takes a stop command's person only from the gateway-signed context."""

    use_gateway_key(monkeypatch)
    yield
    forget_task_context_keys()


async def test_a_stopped_request_frees_its_thread_for_the_next_request(tmp_path: Path) -> None:
    hold_alpha = asyncio.Event()
    turns = (
        _Turn("ALPHA-REQUEST", _answer("Alpha draft."), release=hold_alpha),
        _Turn("BRAVO-REQUEST", _answer("Bravo finished.")),
    )
    async with _team(tmp_path, *turns, request_timeout_seconds=2.0) as team:
        session = team.gateway.application.start_new_session().session_key
        alpha = team.send("ALPHA-REQUEST", session_key=session)
        await asyncio.wait_for(turns[0].started.wait(), WAIT_SECONDS)
        task_alpha = team.root_tasks_named("ALPHA-REQUEST")[0].task_id

        stopped = await team.gateway._operational_controls.stop(
            GatewayStopControlRequest(
                session_key=session,
                action="cancel",
                scope="task",
                target_task_id=task_alpha,
            )
        )
        assert "cancel" in stopped.text.lower(), stopped.text
        await _wait_until(lambda: team.request_for(session, "ALPHA-REQUEST").run_state != "running")
        hold_alpha.set()
        await _settle(alpha)
        await asyncio.sleep(1.0)
        assert team.final_replies.count(task_alpha) == 1, "the stopped turn's one final reply"

        bravo = await asyncio.wait_for(team.send("BRAVO-REQUEST", session_key=session), WAIT_SECONDS)
        assert [task.task_id for task in team.root_tasks_named("BRAVO-REQUEST")] == [bravo.task_id]
        assert team.steering_with("BRAVO-REQUEST") == []
        assert team.lead_model.unexpected == []


async def test_a_stopped_request_that_was_still_waiting_frees_its_thread(tmp_path: Path) -> None:
    """Stopping a request queued behind other work (no turn of it ever ran) frees its thread too."""

    hold_alpha = asyncio.Event()
    # Charlie's prompt also shows the thread's history (Bravo's text), so its
    # turn comes first: the scripted lead takes the first marker it finds.
    turns = (
        _Turn("ALPHA-REQUEST", _answer("Alpha finished."), release=hold_alpha),
        _Turn("CHARLIE-REQUEST", _answer("Charlie finished.")),
        _Turn("BRAVO-REQUEST", _answer("Bravo should not run.")),
    )
    async with _team(tmp_path, *turns, request_timeout_seconds=2.0) as team:
        busy = team.gateway.application.start_new_session().session_key
        alpha = team.send("ALPHA-REQUEST", session_key=busy)
        await asyncio.wait_for(turns[0].started.wait(), WAIT_SECONDS)

        session = team.gateway.application.start_new_session().session_key
        bravo = team.send("BRAVO-REQUEST", session_key=session)
        await _wait_until(lambda: bool(team.root_tasks_named("BRAVO-REQUEST")))
        task_bravo = team.root_tasks_named("BRAVO-REQUEST")[0].task_id
        await _wait_until(
            lambda: team.lead.task_manager.get_task_info(task_bravo) is not None, timeout=WAIT_SECONDS
        )
        assert turns[2].prompt is None, "Bravo waits behind Alpha in the main lane"

        stopped = await team.gateway._operational_controls.stop(
            GatewayStopControlRequest(
                session_key=session,
                action="cancel",
                scope="task",
                target_task_id=task_bravo,
            )
        )
        assert "cancel" in stopped.text.lower(), stopped.text
        await _wait_until(lambda: team.request_for(session, "BRAVO-REQUEST").run_state != "running")
        hold_alpha.set()
        await _settle(alpha, bravo)
        await asyncio.sleep(1.0)
        assert team.final_replies.count(task_bravo) == 1, "the stopped request's one final reply"

        charlie = await asyncio.wait_for(team.send("CHARLIE-REQUEST", session_key=session), WAIT_SECONDS)
        assert [task.task_id for task in team.root_tasks_named("CHARLIE-REQUEST")] == [charlie.task_id]
        assert team.steering_with("CHARLIE-REQUEST") == []
        assert turns[2].prompt is None, "the stopped request never ran"
        assert team.lead_model.unexpected == []
