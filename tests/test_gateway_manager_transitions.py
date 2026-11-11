"""The team lead's availability transitions (assistant_gateway.application.manager_transitions)."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from assistant_gateway.application.manager_client import GatewayManagerTransition
from assistant_gateway.application.manager_transitions import (
    GatewayManagerTransitionApplication,
    GatewayManagerTransitionSettings,
)


def _transitions(waiting: tuple[str, ...]) -> tuple[GatewayManagerTransitionApplication, list[str]]:
    failed: list[str] = []

    async def queue_manager_unavailable(correlation_id: str) -> None:
        failed.append(correlation_id)

    application = GatewayManagerTransitionApplication(
        settings=GatewayManagerTransitionSettings(
            assistant_display_name="Assistant", unavailable_message_template="{assistant_name} is unavailable.",
            available_message_template="{assistant_name} is available.",
            unavailable_message_fallback="unavailable", available_message_fallback="available",
            enabled_fallback_channels=(), telegram_pause_polling=False),
        sessions=SimpleNamespace(), targets=SimpleNamespace(), proactive_sessions=SimpleNamespace(),
        maintenance=SimpleNamespace(ensure_all=AsyncMock()), manager=SimpleNamespace(pending_response_ids=waiting),
        responses=SimpleNamespace(queue_manager_unavailable=queue_manager_unavailable),
        event_output=SimpleNamespace(), logger=logging.getLogger("test.gateway.manager_transitions"))
    return application, failed


def _broken(*args, **kwargs):
    raise RuntimeError("broken")


@pytest.mark.asyncio
async def test_a_failing_notice_or_hook_still_fails_the_waiting_replies(caplog) -> None:
    """Critique C03: each part of a transition runs on its own."""

    application, failed = _transitions(("corr_1", "corr_2"))
    application.enable_notices()
    application._emit_notice = AsyncMock(side_effect=RuntimeError("notice failed"))
    application.set_hooks(on_manager_unavailable=_broken)

    await application.handle_unavailable(GatewayManagerTransition(manager_id="lead_1", available=False,
                                                                  source="agent_records", reason="it left"))

    assert failed == ["corr_1", "corr_2"]
    assert "event=gateway_manager_transition_step_failed step=notice manager_id=lead_1" in caplog.text
    assert "event=gateway_manager_transition_step_failed step=hook manager_id=lead_1" in caplog.text


@pytest.mark.asyncio
async def test_a_failing_available_notice_still_runs_the_hook() -> None:
    application, _ = _transitions(())
    application.start()
    application.enable_notices()
    application._emit_notice = AsyncMock(side_effect=RuntimeError("notice failed"))
    resumed: list[str] = []
    application.set_hooks(on_manager_available=lambda: resumed.append("resume"))

    await application.handle_available(GatewayManagerTransition(manager_id="lead_1", available=True,
                                                                source="agent_records", reason="back",
                                                                became_available=True))
    assert resumed == ["resume"]


@pytest.mark.asyncio
async def test_a_lead_that_still_finishes_its_tasks_leaves_the_waiting_replies_alone() -> None:
    """Review F16: a lead on another contract takes no new chats, but its answers to waiting people still come."""

    application, failed = _transitions(("corr_1",))
    paused: list[str] = []
    application.set_hooks(on_manager_unavailable=lambda: paused.append("pause"))

    await application.handle_unavailable(GatewayManagerTransition(manager_id="lead_1", available=False,
                                                                  source="agent_records", reason="another contract",
                                                                  keep_waiting=True))
    assert failed == [] and paused == ["pause"]
    await application.fail_waiting(["corr_1"])
    assert failed == ["corr_1"]
