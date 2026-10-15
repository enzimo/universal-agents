"""Only TeamArchitect asks the person for credentials; specialists escalate.

Before 2026-10-07 specialists were offered the credential-form and signup
actions. Their requests went to the agent that delegated the task, which had no
handler and dropped them as an unhandled type, and the specialist waited until
the watchdog failed its task. Now specialists get a turn answer without those
actions, a stray request is answered with a refusal that resumes the
specialist's task, and TeamArchitect's own requests go to the person's chat even
while it works on a specialist's escalation.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from custom_agents.message_routing import requester_bound_primary_route
from custom_agents.sdk.strands.turn import LLMTurnExecutor
from custom_agents.structured_llm_messages import (
    CompatibleModelAgentTurnDecision,
    CompatibleWorkerTurnDecision,
    ModelAgentTurnDecision,
    ModelWorkerTurnDecision,
)
from custom_agents.task_message_runtime import (
    DELEGATE_CREDENTIAL_REQUEST_REFUSAL,
    _credential_result_resume_directive,
    refuse_credential_request_from_delegate,
)
from custom_messages.contracts.credentials_v1 import (
    SecureCredentialCollectionRequest,
    SecureCredentialCollectionResult,
    SignupAutomationRequest,
)
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.enums import RequesterType

CREDENTIAL_ACTIONS = ("secure_credential_collection_request", "signup_automation_request")


def _credential_action() -> dict:
    return {
        "action_type": "secure_credential_collection_request",
        "secure_credential_collection_request": {
            "service_name": "Example API",
            "credential_key": "example_api",
            "purpose": "call the API",
        },
    }


def _escalation_action() -> dict:
    return {
        "action_type": "manager_escalation",
        "task": {"problem_statement": "The task needs an Example API key."},
        "escalation": {"escalation_type": "needs_user_input"},
    }


@pytest.mark.parametrize("model", [ModelWorkerTurnDecision, CompatibleWorkerTurnDecision])
def test_specialist_turn_answers_offer_no_credential_actions(model):
    schema = json.dumps(model.model_json_schema())
    for action in CREDENTIAL_ACTIONS:
        assert action not in schema
    with pytest.raises(ValidationError):
        model.model_validate({"actions": [_credential_action()]})
    assert model.model_validate({"actions": [_escalation_action()]}).to_runtime_decision().actions


@pytest.mark.parametrize("model", [ModelAgentTurnDecision, CompatibleModelAgentTurnDecision])
def test_team_lead_turn_answers_keep_credential_actions(model):
    schema = json.dumps(model.model_json_schema())
    for action in CREDENTIAL_ACTIONS:
        assert action in schema
    assert model.model_validate({"actions": [_credential_action()]}).to_runtime_decision().actions


@pytest.mark.parametrize(
    ("is_team_lead", "compatible", "expected"),
    [
        (True, False, ModelAgentTurnDecision),
        (False, False, ModelWorkerTurnDecision),
        (True, True, CompatibleModelAgentTurnDecision),
        (False, True, CompatibleWorkerTurnDecision),
    ],
)
def test_the_turn_answer_follows_the_agent_role(is_team_lead, compatible, expected):
    executor = SimpleNamespace(
        settings=SimpleNamespace(is_team_lead=is_team_lead),
        _uses_compatible_turn_decision_schema=lambda: compatible,
    )
    assert LLMTurnExecutor._turn_decision_structured_output_model(executor) is expected


def _instance(tmp_path: Path) -> tuple[str, str, str]:
    from assistant_gateway.config import GatewayInstanceConfig

    config = GatewayInstanceConfig.create(
        instance_name="creds", instance_root=str(tmp_path / "creds"), team_id="team_creds", timezone="UTC"
    )
    path = tmp_path / "creds" / "instance_config.yaml"
    config.save(path)
    return str(path), config.manager.team_id, config.manager.response_queue_name


@pytest.mark.parametrize(
    "request_content",
    [
        SecureCredentialCollectionRequest(service_name="Example", credential_key="example", purpose="use it"),
        SignupAutomationRequest(service_name="Example", signup_url="https://example.test", credential_key="example"),
    ],
)
def test_the_team_leads_form_request_reaches_the_persons_chat_during_an_escalation(tmp_path, request_content):
    path, team_id, gateway_route = _instance(tmp_path)
    # TeamArchitect works on a specialist's escalation: the specialist is the requester.
    escalation_header = MessageHeader(from_agent_id="worker_1", response_routing_key="worker_1")

    lead_route = requester_bound_primary_route(
        request_content,
        escalation_header,
        instance_config_path=path,
        team_id=team_id,
        requester_type=RequesterType.AGENT_REQUESTER,
        sender_is_team_lead=True,
    )
    specialist_route = requester_bound_primary_route(
        request_content,
        MessageHeader(from_agent_id="lead_1", response_routing_key="lead_1"),
        instance_config_path=path,
        team_id=team_id,
        requester_type=RequesterType.AGENT_REQUESTER,
        sender_is_team_lead=False,
    )

    assert gateway_route and lead_route == gateway_route
    # A stray specialist request goes to the agent that delegated the task, which refuses it.
    assert specialist_route == "lead_1"


class _Comms:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_message(self, **kwargs) -> None:
        self.sent.append(kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "request_content",
    [
        SecureCredentialCollectionRequest(
            service_name="Example", credential_key="example", purpose="use it", parent_task_id="task_w"
        ),
        SignupAutomationRequest(
            service_name="Example", signup_url="https://example.test", credential_key="example", parent_task_id="task_w"
        ),
    ],
)
async def test_a_credential_request_from_a_delegate_is_refused_back_to_it(caplog, request_content):
    comms = _Comms()
    lead = SimpleNamespace(id="lead_1", name="TeamArchitect-1", team_comms=comms, _logger=logging.getLogger("test.refusal"))
    header = MessageHeader(from_agent_id="worker_1", response_routing_key="worker_1")
    meta = MessageMeta(msg_correlation_id="corr_1", conversation_id="conv_1")

    with caplog.at_level(logging.WARNING, logger="test.refusal"):
        await refuse_credential_request_from_delegate(lead, request_content, header, meta)

    assert len(comms.sent) == 1
    sent = comms.sent[0]
    envelope = sent["message"]
    refusal = envelope.msg_content
    assert sent["routing_key"] == "worker_1" and envelope.msg_header.to_routing_path == "worker_1"
    assert isinstance(refusal, SecureCredentialCollectionResult)
    assert refusal.credential_request_id == request_content.get_id()
    assert refusal.status == "credential_unavailable" and refusal.continue_task is True
    assert refusal.parent_task_id == "task_w" and refusal.error == DELEGATE_CREDENTIAL_REQUEST_REFUSAL
    assert envelope.msg_meta.msg_correlation_id == "corr_1"
    assert any("event=delegate_credential_request_refused" in r.getMessage() for r in caplog.records)


def test_the_refusal_resumes_the_specialist_with_the_escalation_guidance():
    refusal = SecureCredentialCollectionResult(
        credential_request_id="cred_req_1",
        status="credential_unavailable",
        credential_key="example",
        service_name="Example",
        parent_task_id="task_w",
        error=DELEGATE_CREDENTIAL_REQUEST_REFUSAL,
        continue_task=True,
    )

    directive = _credential_result_resume_directive(refusal)

    assert "manager_escalation" in directive and "needs_user_input" in directive
