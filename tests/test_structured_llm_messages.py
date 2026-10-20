from __future__ import annotations


import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from strands.tools.structured_output.structured_output_tool import convert_pydantic_to_tool_spec

from custom_agents.sdk.strands import turn as llm_turn_runtime
from custom_agents.agent_instructions import shared_agent_base_prompt
from custom_agents.structured_llm_messages import (
    CompatibleModelAgentTurnDecision,
    FireworksModelAgentTurnDecision,
    MODEL_AGENT_TURN_DECISION_FORCE_PROMPT,
    ModelAgentTurnDecision,
    ModelTaskQueryAction,
    ModelTaskQueryPayload,
    ModelTaskResponseAction,
    SecureCredentialCollectionAction,
    StructuredAgentTurnDecision,
    TaskQueryAction,
)
from custom_messages.enums import MessageMetaType


@pytest.mark.parametrize("decision_type", [ModelAgentTurnDecision, StructuredAgentTurnDecision])
@pytest.mark.parametrize("ending", ["schedule", "noop"])
def test_schedule_batch_needs_a_final_response_or_wait(decision_type, ending):
    payload = {"problem_statement": "Check the service later."}
    if decision_type is StructuredAgentTurnDecision:
        payload["task_type"] = "request"
    actions = [{
        "action_type": "schedule_message",
        "scheduled_message": {"payload": payload, "interval_ms": 60_000},
    }]
    if ending == "noop":
        actions.append({"action_type": "noop", "reason": "The schedule was saved."})
    with pytest.raises(ValidationError, match="schedule_message.*followed"):
        decision_type.model_validate({"actions": actions})

    actions.append({
        "action_type": "task_response",
        "task": {"answer": "I scheduled the check.", **({"task_type": "response"} if decision_type is StructuredAgentTurnDecision else {})},
    })
    decision = decision_type.model_validate({"actions": actions})
    assert decision.actions[-1].task.answer == "I scheduled the check."


_SECURE_CREDENTIAL_SKILL = (
    Path(__file__).resolve().parent.parent
    / "custom_agents"
    / "skill_templates"
    / "secure-credential-workflows"
    / "SKILL.md"
)
_SHELL_EXECUTION_SKILL = (
    Path(__file__).resolve().parents[1]
    / "custom_agents"
    / "skill_templates"
    / "shell-execution-workflows"
    / "SKILL.md"
)


def test_model_task_response_action_normalizes_flat_answer_payload() -> None:
    action = ModelTaskResponseAction.model_validate(
        {
            "action_type": "task_response",
            "answer": "Done from a flat compact response.",
            "task_plan": "Reported current credential state.",
            "thoughts_info": ["Checked the current store state."],
        }
    )

    assert action.task.answer == "Done from a flat compact response."
    assert action.task.task_plan == "Reported current credential state."
    assert action.task.thoughts_info == ["Checked the current store state."]


def test_model_task_response_action_rejects_runtime_owned_ids_after_normalization() -> None:
    with pytest.raises(ValidationError):
        ModelTaskResponseAction.model_validate(
            {
                "action_type": "task_response",
                "answer": "This should still fail.",
                "task_id": "model_authored_task_id",
            }
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"query_type": MessageMetaType.REQUEST},
        {"query_type": MessageMetaType.REQUEST, "message": ""},
        {"query_type": MessageMetaType.REQUEST, "message": "   \n\t"},
    ],
)
def test_model_task_query_payload_requires_non_empty_message(payload: dict) -> None:
    with pytest.raises(ValidationError):
        ModelTaskQueryPayload.model_validate(payload)


def test_model_task_query_payload_strips_message_whitespace() -> None:
    payload = ModelTaskQueryPayload(
        query_type=MessageMetaType.REQUEST,
        message="  Which provider should I test first?  ",
    )

    assert payload.message == "Which provider should I test first?"


def test_model_agent_turn_decision_schema_is_smaller_than_runtime_schema() -> None:
    model_schema = json.dumps(ModelAgentTurnDecision.model_json_schema(), sort_keys=True)
    runtime_schema = json.dumps(StructuredAgentTurnDecision.model_json_schema(), sort_keys=True)

    assert "StructuredLLMResponse" not in model_schema
    assert len(model_schema) < int(len(runtime_schema) * 0.85)


def _schema_paths_with_key(schema: object, key: str, path: str = "") -> list[str]:
    paths: list[str] = []
    if isinstance(schema, dict):
        if key in schema:
            paths.append(path or "<root>")
        for child_key, child_value in schema.items():
            paths.extend(_schema_paths_with_key(child_value, key, f"{path}.{child_key}" if path else str(child_key)))
    elif isinstance(schema, list):
        for index, child_value in enumerate(schema):
            paths.extend(_schema_paths_with_key(child_value, key, f"{path}[{index}]"))
    return paths


def _schema_paths_missing_type(schema: object, path: str = "") -> list[str]:
    paths: list[str] = []
    if isinstance(schema, dict):
        looks_like_property = (
            ("description" in schema or "title" in schema)
            and not any(key in schema for key in ("type", "anyOf", "allOf", "oneOf", "$ref", "enum", "const"))
        )
        if looks_like_property and path and not path.endswith(".properties"):
            paths.append(path)
        for child_key, child_value in schema.items():
            if child_key in {"description", "title", "default"}:
                continue
            paths.extend(_schema_paths_missing_type(child_value, f"{path}.{child_key}" if path else str(child_key)))
    elif isinstance(schema, list):
        for index, child_value in enumerate(schema):
            paths.extend(_schema_paths_missing_type(child_value, f"{path}[{index}]"))
    return paths


def test_compatible_turn_decision_schema_retains_safe_scalar_constraints() -> None:
    flattened_tool_schema = convert_pydantic_to_tool_spec(
        CompatibleModelAgentTurnDecision
    )["inputSchema"]["json"]
    action_properties = flattened_tool_schema["properties"]["actions"]["items"]["properties"]

    assert "selected_message_type" in flattened_tool_schema["required"]
    assert flattened_tool_schema["properties"]["selected_message_type"] == {
        "description": "Always `AgentTurnDecision` for runtime action decisions.",
        "enum": ["AgentTurnDecision"],
        "title": "Selected Message Type",
        "type": "string",
    }
    assert action_properties["action_type"]["enum"] == [
        "delegate_task",
        "manager_escalation",
        "task_response",
        "task_query",
        "schedule_message",
        "secure_credential_collection_request",
        "signup_automation_request",
        "noop",
    ]
    assert "capacity_policy" not in action_properties
    assert "requester_type" not in action_properties

    canonical_delegate_properties = ModelAgentTurnDecision.model_json_schema()["$defs"][
        "ModelDelegateTaskAction"
    ]["properties"]
    assert "capacity_policy" not in canonical_delegate_properties
    assert "requester_type" not in canonical_delegate_properties


def test_compatible_turn_decision_rejects_model_authored_requester_type() -> None:
    with pytest.raises(ValidationError, match="requester_type"):
        CompatibleModelAgentTurnDecision.model_validate(
            {
                "actions": [
                    {
                        "action_type": "delegate_task",
                        "task": {"problem_statement": "Compare the Volvo XC90 with competitors."},
                        "target_named_agent_type": "ResearchComparisonAgent",
                        "requester_type": "user_request",
                    }
                ]
            }
        )


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("action_type", "delegate_request"),
        ("capacity_policy", "best_effort"),
    ],
)
def test_compatible_turn_decision_rejects_invalid_scalar_contract_values(
    field_name: str,
    invalid_value: str,
) -> None:
    action = {
        "action_type": "delegate_task",
        "task": {"problem_statement": "Compare the Volvo XC90 with competitors."},
        "target_named_agent_type": "ResearchComparisonAgent",
    }
    action[field_name] = invalid_value

    with pytest.raises(ValidationError, match=field_name):
        CompatibleModelAgentTurnDecision.model_validate({"actions": [action]})


def test_compatible_turn_decision_runs_canonical_validation_during_wrapper_validation() -> None:
    with pytest.raises(ValidationError, match="canonical ModelAgentTurnDecision contract"):
        CompatibleModelAgentTurnDecision.model_validate(
            {
                "actions": [
                    {
                        "action_type": "task_response",
                        "task": {"problem_statement": "This is not a valid response payload."},
                    }
                ]
            }
        )


def test_fireworks_turn_decision_wrapper_supports_task_query() -> None:
    wrapped = FireworksModelAgentTurnDecision.model_validate(
        {
            "actions": [
                {
                    "action_type": "task_query",
                    "task_query": {
                        "query_type": "request",
                        "message": "Which credential should I store?",
                    },
                }
            ],
            "summary_for_history": "Asked for the missing credential target.",
        }
    )

    canonical = wrapped.to_model_agent_turn_decision()
    runtime = wrapped.to_runtime_decision()

    canonical_action = canonical.actions[0]
    runtime_action = runtime.actions[0]
    assert isinstance(canonical_action, ModelTaskQueryAction)
    assert canonical_action.task_query.query_type == MessageMetaType.REQUEST
    assert canonical_action.task_query.message == "Which credential should I store?"
    assert isinstance(runtime_action, TaskQueryAction)
    assert runtime_action.task_query.query_type == MessageMetaType.REQUEST
    assert runtime_action.task_query.message == "Which credential should I store?"


def test_fireworks_turn_decision_wrapper_rejects_empty_task_query_payload() -> None:
    with pytest.raises(ValidationError, match="query_type"):
        FireworksModelAgentTurnDecision.model_validate(
            {
                "actions": [
                    {
                        "action_type": "task_query",
                        "task_query": {},
                    }
                ],
            }
        )


def test_fireworks_turn_decision_wrapper_rejects_missing_task_query_payload() -> None:
    with pytest.raises(ValidationError, match="task_query required"):
        FireworksModelAgentTurnDecision.model_validate(
            {
                "actions": [
                    {
                        "action_type": "task_query",
                    }
                ],
            }
        )


def test_fireworks_turn_decision_wrapper_rejects_invalid_task_query_nested_fields() -> None:
    with pytest.raises(ValidationError, match="query_reason"):
        FireworksModelAgentTurnDecision.model_validate(
            {
                "actions": [
                    {
                        "action_type": "task_query",
                        "task_query": {
                            "query_type": "request",
                            "message": "Which credential should I store?",
                            "query_reason": "missing_non_secret_credential_metadata",
                        },
                    }
                ],
            }
        )

    with pytest.raises(ValidationError, match="option_id"):
        FireworksModelAgentTurnDecision.model_validate(
            {
                "actions": [
                    {
                        "action_type": "task_query",
                        "task_query": {
                            "query_type": "request",
                            "message": "Which credential should I store?",
                            "choices": [{}],
                        },
                    }
                ],
            }
        )


def test_fireworks_turn_decision_wrapper_rejects_non_noop_reason() -> None:
    with pytest.raises(ValidationError, match="reason may only be set"):
        FireworksModelAgentTurnDecision.model_validate(
            {
                "actions": [
                    {
                        "action_type": "task_query",
                        "task_query": {
                            "query_type": "request",
                            "message": "Which credential should I store?",
                        },
                        "reason": "Need non-secret metadata before credential collection.",
                    }
                ],
            }
        )


def test_fireworks_turn_decision_wrapper_supports_secure_credential_request() -> None:
    wrapped = FireworksModelAgentTurnDecision.model_validate(
        {
            "actions": [
                {
                    "action_type": "secure_credential_collection_request",
                    "secure_credential_collection_request": {
                        "service_name": "GitHub",
                        "credential_key": "github_pat",
                        "purpose": "GitHub API access for approved workflows.",
                        "fields": [
                            {
                                "name": "api_token",
                                "kind": "token",
                                "visibility": "secret",
                                "value_source": "user_input",
                                "required": True,
                                "label": "GitHub personal access token",
                            }
                        ],
                    },
                }
            ],
        }
    )

    runtime = wrapped.to_runtime_decision()

    action = runtime.actions[0]
    assert isinstance(action, SecureCredentialCollectionAction)
    assert action.secure_credential_collection_request.service_name == "GitHub"
    assert action.secure_credential_collection_request.credential_key == "github_pat"
    assert action.secure_credential_collection_request.fields[0].name == "api_token"


def test_fireworks_turn_decision_wrapper_rejects_empty_secure_credential_request() -> None:
    with pytest.raises(ValidationError, match="service_name"):
        FireworksModelAgentTurnDecision.model_validate(
            {
                "actions": [
                    {
                        "action_type": "secure_credential_collection_request",
                        "secure_credential_collection_request": {},
                    }
                ],
            }
        )


def test_fireworks_turn_decision_wrapper_rejects_response_alias_for_answer() -> None:
    with pytest.raises(ValidationError, match="response"):
        FireworksModelAgentTurnDecision.model_validate(
            {
                "actions": [
                    {
                        "action_type": "task_response",
                        "task": {"response": "Use answer instead."},
                    }
                ],
            }
        )


def test_initial_turn_guidance_names_the_selected_terminal_tool() -> None:
    compatible_guidance = llm_turn_runtime._turn_decision_initial_guidance(
        CompatibleModelAgentTurnDecision.__name__
    )
    canonical_guidance = llm_turn_runtime._turn_decision_initial_guidance(ModelAgentTurnDecision.__name__)

    assert "call exactly `CompatibleModelAgentTurnDecision`" in compatible_guidance
    assert "call exactly `ModelAgentTurnDecision`" in canonical_guidance
    assert "use regular tools as needed" in compatible_guidance
    assert "actions[].task.answer" in compatible_guidance


def test_force_prompt_enumerates_non_answer_runtime_actions() -> None:
    force_prompt = MODEL_AGENT_TURN_DECISION_FORCE_PROMPT

    assert "Do not answer in prose" in force_prompt
    assert 'action_type="task_response"' in force_prompt
    assert 'action_type="delegate_task"' in force_prompt
    assert 'action_type="manager_escalation"' in force_prompt
    assert 'action_type="task_query"' in force_prompt
    assert 'query_type="request"' in force_prompt
    assert 'action_type="secure_credential_collection_request"' in force_prompt
    assert 'action_type="schedule_message"' in force_prompt
    assert 'action_type="signup_automation_request"' in force_prompt
    assert 'action_type="noop"' in force_prompt
    assert "If you already drafted prose or Markdown" in force_prompt
    assert "If you wrote, updated, saved, or generated" in force_prompt
    assert "summarize the file path" in force_prompt


def test_credential_guidance_routes_missing_details_through_task_query() -> None:
    skill_text = _SECURE_CREDENTIAL_SKILL.read_text(encoding="utf-8")

    assert "When a workflow needs a new named credential" in skill_text
    assert "ask through a terminal" in skill_text
    assert "`task_query` action" in skill_text
    assert "`github_pat` template" in skill_text
    assert '"credential_key": "porkbun_api"' in skill_text
    assert "binding_name" not in skill_text
    assert "`account_link_missing` means no account is linked" in skill_text
    assert '"provider": "secure_requests"' in skill_text
    assert '"json_body"' in skill_text
    assert "additional.renewal" in skill_text
    assert 'json_body: {"tlds": ["xyz"]}' in skill_text
    assert "do not fetch the entire pricing catalog" in skill_text
    assert "Do not repeat an identical broker validation failure" in skill_text
    assert "secure-credential-workflows" in shared_agent_base_prompt


def test_shell_execution_skill_documents_runtime_contract() -> None:
    skill_text = _SHELL_EXECUTION_SKILL.read_text(encoding="utf-8")

    assert "Do not prefix it with `rtk`" in skill_text
    assert "parallel=true" in skill_text
    assert "ignore_errors=true" in skill_text
    assert '"work_dir"' in skill_text
    assert "closed stdin and no PTY" in skill_text
    assert "child process tree" in skill_text
    assert "rtk_mode=rewrite" in skill_text
    assert "rtk_mode=proxy" in skill_text
    assert "rtk_mode=missing" in skill_text
    assert "timeout` uses exit code 124" in skill_text
    assert "cancelled` uses exit code 130" in skill_text
    assert "localhost or loopback services" in skill_text


@pytest.mark.parametrize(
    "model",
    [ModelAgentTurnDecision, CompatibleModelAgentTurnDecision, FireworksModelAgentTurnDecision],
)
def test_the_model_cannot_open_setup_pages_itself(model) -> None:
    """Setup pages open only from set_up_tools, in trusted code (owner's decision 2026-09-28)."""

    action = {
        "action_type": "tool_setup_request",
        "tool_setup_request": {"tool_id": "cli:github", "kind": "sign_in", "reason": "Sign in once."},
    }

    with pytest.raises(ValidationError):
        model.model_validate({"actions": [action]})
