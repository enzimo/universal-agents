"""The team lead's availability transitions (assistant_gateway.application.manager_transitions)."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from assistant_gateway.application.manager_client import GatewayManagerTransition
from assistant_gateway.application.manager_transitions import (
    GatewayManagerTransitionApplication,
    GatewayManagerTransitionSettings,
)


def _transitions(waiting: tuple[str, ...]) -> tuple[GatewayManagerTransitionApplication, list[str]]:
    return transition_service(SimpleNamespace(pending_response_ids=waiting))


def transition_service(manager: Any, *, kept_results: Any = None) -> tuple[GatewayManagerTransitionApplication, list[str]]:
    """A transition service over ``manager``'s waiting replies (``pending_response_ids``) and the replies it fails,
    in order. Every failure is recorded, so a reply failed twice shows twice; also used by the lead follower's
    tests (tests/test_gateway_team_view.py). ``kept_results`` stands in for the setup results kept for the team
    lead (``tool_setup.GatewayToolSetups``); by default it sends nothing."""

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
        maintenance=SimpleNamespace(ensure_all=AsyncMock()), manager=manager,
        kept_results=kept_results or SimpleNamespace(resend_kept_results=AsyncMock()),
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
async def test_the_replies_a_lead_on_another_contract_still_answers_are_kept_and_the_others_failed(caplog) -> None:
    """Review F16 and R1: a lead on another contract takes no new chats, but its answers to waiting people still come;
    the notice and the hook still run, and every other waiting reply is failed."""

    caplog.set_level(logging.INFO)
    application, failed = _transitions(("corr_kept", "corr_other"))
    application.enable_notices()
    application._emit_notice = AsyncMock()
    paused: list[str] = []
    application.set_hooks(on_manager_unavailable=lambda: paused.append("pause"))

    await application.handle_unavailable(GatewayManagerTransition(
        manager_id="lead_1", available=False, source="agent_records", reason="another contract",
        kept_replies=frozenset({"corr_kept", "corr_already_answered"})))
    assert failed == ["corr_other"] and paused == ["pause"]
    application._emit_notice.assert_awaited_once_with(available=False)
    assert "event=gateway_manager_waits_kept manager_id=lead_1 kept=1 failed=1" in caplog.text
    await application.fail_waiting(["corr_kept"])
    assert failed == ["corr_other", "corr_kept"]


def _arrival(*, back: bool) -> GatewayManagerTransition:
    return GatewayManagerTransition(manager_id="lead_1", available=True, source="agent_records", reason="back",
                                    became_available=back)


@pytest.mark.asyncio
@pytest.mark.parametrize("failing", ["notice", "hook"])
async def test_a_failing_notice_or_hook_still_sends_the_kept_setup_results(caplog, failing) -> None:
    """Review C01: main sent kept setup results in a ``finally`` after the notice and the hook; each part now runs
    in ``_step``, so a notice or hook that fails must still not skip the send, and the failure is logged."""

    kept = SimpleNamespace(resend_kept_results=AsyncMock())
    application, _ = transition_service(SimpleNamespace(pending_response_ids=()), kept_results=kept)
    application.start()
    application.enable_notices()
    application._emit_notice = AsyncMock(side_effect=RuntimeError("notice failed") if failing == "notice" else None)
    application.set_hooks(on_manager_available=_broken if failing == "hook" else None)

    await application.handle_available(_arrival(back=True))

    kept.resend_kept_results.assert_awaited_once_with()
    assert f"event=gateway_manager_transition_step_failed step={failing} manager_id=lead_1" in caplog.text


@pytest.mark.asyncio
async def test_a_confirmed_lead_sends_only_the_kept_setup_results(caplog) -> None:
    """The records name the lead chats already go to (GatewayManagerClient.confirm_lead): no schedule check, notice
    or hook, only the kept results; nothing before the transitions start, and a failed send is logged."""

    kept = SimpleNamespace(resend_kept_results=AsyncMock())
    application, _ = transition_service(SimpleNamespace(pending_response_ids=()), kept_results=kept)
    application.enable_notices()
    application._emit_notice = AsyncMock()
    hooks: list[str] = []
    application.set_hooks(on_manager_available=lambda: hooks.append("resume"))

    await application.handle_lead_confirmed(_arrival(back=False))  # Not started yet: the gateway's start decision.
    kept.resend_kept_results.assert_not_awaited()

    application.start()
    await application.handle_lead_confirmed(_arrival(back=False))
    kept.resend_kept_results.assert_awaited_once_with()
    application._maintenance.ensure_all.assert_not_awaited()
    application._emit_notice.assert_not_awaited()
    assert hooks == []

    kept.resend_kept_results.side_effect = RuntimeError("store unavailable")
    await application.handle_lead_confirmed(_arrival(back=False))
    assert "event=gateway_manager_transition_step_failed step=resend_kept_results manager_id=lead_1" in caplog.text
