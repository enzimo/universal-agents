"""Exercise prose recovery through the SDK with role-specific turn decisions."""

import json
from types import SimpleNamespace

import pytest
from strands import Agent
from strands.models import Model

from tests.llm_turn_fixtures import turn_executor_for


class RecoveryModel(Model):
    """Follow the recovery instructions while honoring the forced tool name."""

    def __init__(self, need):
        self.need = need
        self.calls = []

    def update_config(self, **model_config):
        pass

    def get_config(self):
        return {"model_id": "turn-recovery-test"}

    async def structured_output(self, *args, **kwargs):
        raise AssertionError("Recovery must use the SDK structured-output tool")
        yield

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        self.calls.append(kwargs.get("tool_choice"))
        yield {"messageStart": {"role": "assistant"}}
        if len(self.calls) == 1:
            yield {"contentBlockDelta": {"delta": {"text": "Account setup is needed."}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "end_turn"}}
            return

        prompt = "\n".join(block.get("text", "") for block in messages[-1]["content"])
        tool_name = kwargs["tool_choice"]["tool"]["name"]
        assert f"Call the `{tool_name}` structured-output tool now" in prompt
        assert tool_name in {spec["name"] for spec in tool_specs}
        if self.need == "answer":
            action = {"action_type": "task_response", "task": {"answer": "Done."}}
        elif f'action_type="{self.need}"' in prompt:
            payload = {"service_name": "Example", "credential_key": "example"}
            if self.need == "signup_automation_request":
                payload["signup_url"] = "https://example.test/signup"
            else:
                payload["purpose"] = "Call Example API"
            action = {"action_type": self.need, self.need: payload}
        else:
            assert 'action_type="manager_escalation"' in prompt
            assert "needs_user_input" in prompt
            action = {
                "action_type": "manager_escalation",
                "task": {"problem_statement": "Set up the Example account."},
                "escalation": {"escalation_type": "needs_user_input"},
            }
        yield {
            "contentBlockStart": {
                "start": {"toolUse": {"toolUseId": "recovery", "name": tool_name}}
            }
        }
        yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps({"actions": [action]})}}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "tool_use"}}


@pytest.mark.parametrize("is_team_lead", [False, True])
@pytest.mark.parametrize("compatible", [False, True])
@pytest.mark.parametrize("need", ["secure_credential_collection_request", "signup_automation_request", "answer"])
@pytest.mark.asyncio
async def test_prose_recovery_uses_role_allowed_actions(is_team_lead, compatible, need):
    executor = turn_executor_for(
        SimpleNamespace(
            is_team_lead=is_team_lead,
            _uses_compatible_turn_decision_schema=lambda: compatible,
        )
    )
    decision_model = executor._turn_decision_structured_output_model()
    prompt = executor._turn_decision_structured_output_prompt()
    if not is_team_lead:
        assert "secure_credential_collection_request" not in prompt
        assert "signup_automation_request" not in prompt
    model = RecoveryModel(need)
    agent = Agent(model=model, callback_handler=None)

    result = await agent.invoke_async(
        "Finish the delegated task.",
        structured_output_model=decision_model,
        structured_output_prompt=prompt,
    )

    assert isinstance(result.structured_output, decision_model)
    assert len(model.calls) == 2  # Prose followed by one valid recovery, without retries.
    action = result.structured_output.actions[0]
    expected = "task_response" if need == "answer" else need if is_team_lead else "manager_escalation"
    assert action.action_type == expected
    runtime_action = result.structured_output.to_runtime_decision().actions[0]
    if expected == "manager_escalation":
        assert runtime_action.target_named_agent_type == "TeamArchitect"
        assert runtime_action.manager_escalation.escalation_type == "needs_user_input"
        # Every delegation, an escalation included, makes the task wait for its answer.
        from custom_agents.turn_work_notes import decision_starts_wait

        assert decision_starts_wait(result.structured_output.to_runtime_decision())
    elif expected == "task_response":
        assert runtime_action.task.answer == "Done."
