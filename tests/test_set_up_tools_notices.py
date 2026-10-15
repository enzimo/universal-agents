"""Regressions from a live run: "What private repos can you access in GitHub?" (2026-09-28).

Access and the GitHub sign-in were both missing, so ``set_up_tools`` published a
workflow. The person saw the sign-in card and the access review, but:

1. nothing in chat told them a workflow existed or linked it: the gateway put
   the link only in the reply's ``text``, which Web Chat never shows for a
   setup card (the card shows its description, the transcript its receipt);
2. right after the access approval the workflow logged
   ``agent_reported_complete``: trusted code closed the access step through
   the agent's progress path, so it read as if TeamLead reported it;
3. (checked) the task must keep waiting for the sign-in after the approval.

Covered: one clear chat notice with the full workflow address, before any
page opens and only once; its text reaches Telegram and Discord unchanged; the
setup steps' events are trusted code's own (``access_requested``,
``access_approved``), never ``agent_reported_*``; the task stays waiting.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from assistant_gateway.application.delivery_presentation import GatewayDeliveryPresentation
from assistant_gateway.application.gateway_application import GatewayDeliveryEvent
from assistant_gateway.interactive_apps.delivery_projection import project_flattened_delivery, project_text_delivery
from custom_agents import access_runtime
from custom_agents.access_workflows import workflow_service_for
from custom_agents.task_human_interaction_blockers import get_human_interaction_blocker
from custom_messages.contracts.access_v2 import AccessReviewChoice
from custom_messages.contracts.credentials_v1 import ToolSetupRequestV1
from custom_messages.contracts.delivery_v1 import UserNotification
from custom_messages.enums import ProgressState
from tests.access_workflow_support import TEAM, needs_cedar, use_signed_contexts
from tests.test_set_up_tools import GITHUB, SIGNED_OUT, start, store
from tests.tool_setup_run_support import broker_states, decide, lead_agent, pause_turn, sent_pages, the_run

pytestmark = needs_cedar
PUBLIC = "https://agents.example.com:8443"
PURPOSE = "list the private repos you can access in GitHub"


@pytest.fixture(autouse=True)
def _signed(monkeypatch: pytest.MonkeyPatch):
    use_signed_contexts(monkeypatch)
    monkeypatch.setenv("INTERNAL_WEBSITE_BASE_URL", PUBLIC)
    yield
    access_runtime._runtime_access.cache_clear()


def sent(agent) -> list:
    return [call.kwargs["message"].msg_content for call in agent.team_comms.send_message.await_args_list]


async def github_run(tmp_path: Path, monkeypatch):
    lead = lead_agent(tmp_path)
    broker_states(monkeypatch, {GITHUB: SIGNED_OUT})
    _call, raised = start(lead, [GITHUB], purpose=PURPOSE)
    await pause_turn(lead, raised.interrupt)
    return lead


@pytest.mark.asyncio
async def test_the_person_is_told_about_the_workflow_once_with_its_full_address(tmp_path: Path, monkeypatch) -> None:
    lead = await github_run(tmp_path, monkeypatch)

    messages = sent(lead)
    notices = [item for item in messages if isinstance(item, UserNotification)]
    [notice] = notices
    workflow_url = f"{PUBLIC}/workflows/{the_run(lead).workflow_id}"
    assert workflow_url in notice.text
    assert notice.text.startswith("I need 2 things before I can list the private repos you can access in GitHub:")
    assert "Access to GitHub" in notice.text and "Sign in to GitHub" in notice.text
    assert isinstance(messages[0], UserNotification), "the link comes before any page"
    assert isinstance(messages[1], ToolSetupRequestV1)
    # A later wake-up (another result) never repeats it.
    [request] = store(lead).list_requests(TEAM)
    await decide(lead, request.request_id, AccessReviewChoice.APPROVE)
    assert len([item for item in sent(lead) if isinstance(item, UserNotification)]) == 1


@pytest.mark.asyncio
async def test_the_notice_reads_the_same_in_web_chat_telegram_and_discord(tmp_path: Path, monkeypatch) -> None:
    lead = await github_run(tmp_path, monkeypatch)
    [envelope] = [
        call.kwargs["message"]
        for call in lead.team_comms.send_message.await_args_list
        if isinstance(call.kwargs["message"].msg_content, UserNotification)
    ]
    notice = envelope.msg_content
    presentation = GatewayDeliveryPresentation()
    text = presentation.render_envelope(envelope)
    # Web Chat: the person's own chat session receives it, rendered as text with the link.
    assert envelope.msg_meta.gateway_context.session_key == "s-1"
    event = GatewayDeliveryEvent(
        text=text, session_key="s", channel_name="telegram", user_id="u", chat_id="c", thread_id=None,
        conversation_id="conv-1", scheduled_job_id=None, correlation_id="corr", original_message_id=None,
        message_type=presentation.message_type(notice), attachments=(),
    )
    workflow_url = f"{PUBLIC}/workflows/{the_run(lead).workflow_id}"

    assert presentation.message_type(notice) == "user_notification"
    assert workflow_url in project_text_delivery(event, channel_name="telegram").text
    assert workflow_url in project_text_delivery(event, channel_name="discord").text
    assert workflow_url in project_flattened_delivery(event, channel_name="signal").text


@pytest.mark.asyncio
async def test_one_piece_gets_no_workflow_notice(tmp_path: Path, monkeypatch) -> None:
    lead = lead_agent(tmp_path, enabled=False)
    broker_states(monkeypatch, {GITHUB: SIGNED_OUT})
    _call, raised = start(lead, [GITHUB])
    await pause_turn(lead, raised.interrupt)

    assert not [item for item in sent(lead) if isinstance(item, UserNotification)]
    assert len(sent_pages(lead)) == 1


@pytest.mark.asyncio
async def test_setup_steps_are_logged_as_trusted_codes_work_not_the_agents(tmp_path: Path, monkeypatch) -> None:
    lead = await github_run(tmp_path, monkeypatch)
    run = the_run(lead)
    [request] = store(lead).list_requests(TEAM)

    await decide(lead, request.request_id, AccessReviewChoice.APPROVE)

    service = workflow_service_for(lead)
    events = service.repository.events(run.workflow_id, offset=0, limit=50)
    codes = [event.code for event in events]
    assert not [code for code in codes if code.startswith("agent_reported_")], codes
    assert "access_requested" in codes and "access_approved" in codes
    record = service.repository.get(run.workflow_id)
    assert record.step("access_github").state.value == "complete"
    assert record.step("sign_in_github").state.value == "waiting"
    assert record.phase.value != "complete", "not complete while the sign-in and the work are open"


@pytest.mark.asyncio
async def test_after_the_access_approval_the_task_keeps_waiting_for_the_sign_in(tmp_path: Path, monkeypatch) -> None:
    lead = await github_run(tmp_path, monkeypatch)
    [request] = store(lead).list_requests(TEAM)

    await decide(lead, request.request_id, AccessReviewChoice.APPROVE)

    manager = lead.task_manager
    assert manager.get_task_info("task-lead").state == ProgressState.WAITING
    assert get_human_interaction_blocker(manager, "task-lead") is not None
    assert not lead._pending_interrupt_responses and manager.consume_runtime_resume_directive("task-lead") is None
    lead._continue_pending_work.assert_not_awaited()
