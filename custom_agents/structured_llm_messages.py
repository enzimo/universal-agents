"""
Structured payload contracts used for Strands structured output.
"""

from __future__ import annotations

from typing import Annotated, Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from pydantic.json_schema import SkipJsonSchema

from custom_messages.enums import (
    RecoverablePauseTrigger,
    ScheduleManagementClass,
    TaskQueryDecision,
    TaskQueryReason,
)

from custom_messages.contracts.control_v1 import AgentStatus, ControlInstruction, NoOpMessage, SystemStatusMessage
from custom_messages.contracts.credentials_v1 import (
    SecureCredentialCollectionRequest,
    SecureCredentialCollectionResult,
    SignupAutomationRequest,
    SignupAutomationResult,
)
from custom_messages.contracts.envelope_v1 import CommsMessage
from custom_messages.contracts.routing_v1 import ManagerEscalationContext, MessageMeta
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.contracts.task_v1 import AfterActionReport, Task, TaskAnswer, TaskQueryChoiceOption, TaskQuery
from custom_messages.enums import MessageMetaType, RequesterType
from custom_messages.contracts.runtime_v1 import TEAM_ARCHITECT_AGENT_TYPE


SCHEDULE_REQUIRES_CONTINUATION = (
    "schedule_message must be followed by a task_response or an action that waits for a result."
)


def _validate_action_batch_end(actions: List[Any]) -> None:
    """A saved schedule is a side effect, not an answer to the accepted task."""
    if not any(action.action_type == "schedule_message" for action in actions):
        return
    continuations = {
        "task_response", "delegate_task", "manager_escalation", "task_query",
        "secure_credential_collection_request", "signup_automation_request",
    }
    ending = actions[-1]
    if ending.action_type not in continuations or (
        ending.action_type == "delegate_task" and not ending.is_response_requested
    ):
        raise ValueError(SCHEDULE_REQUIRES_CONTINUATION)


class StructuredTaskPayload(BaseModel):
    """
    Direct contract for a `Task` payload.

    Fill only the semantic task fields. Do not try to provide runtime-managed
    progress history.
    """

    message_class: Literal["Task"] = Field(default="Task", description="Always `Task`.")
    task_id: Optional[str] = Field(default=None, description="Reuse the existing task id when replying.")
    parent_task_id: Optional[str] = Field(default=None, description="Parent task id for subtasks.")
    task_type: MessageMetaType = Field(..., description="Required. `request` or `response`.")
    problem_statement: Optional[str] = Field(default=None, description="Required for task requests.")
    errors: Optional[List[str]] = Field(default=None, description="Optional error list.")
    task_plan: Optional[str] = Field(default=None, description="Optional high-level plan.")
    thoughts_info: Optional[Union[List[Any], str]] = Field(
        default=None,
        description="Optional notes relevant to the task.",
    )
    answer: Optional[Union[TaskAnswer, str]] = Field(
        default=None,
        description="Required for task responses. Prefer a `TaskAnswer` object.",
    )

    def to_task(self) -> Task:
        task_kwargs = self.model_dump(exclude_none=True)
        return Task(**task_kwargs)


class StructuredScheduledMessagePayload(BaseModel):
    """Direct contract for a `ScheduledMessage` payload."""

    message_class: Literal["ScheduledMessage"] = Field(
        default="ScheduledMessage",
        description="Always `ScheduledMessage`.",
    )
    schedule_id: Optional[str] = Field(default=None, description="Optional schedule id.")
    payload: StructuredTaskPayload = Field(description="Required. The task to run when the schedule fires.")
    interval_ms: Optional[int] = Field(
        default=None,
        description="Optional fixed repeat interval in milliseconds.",
    )
    cron_expression: Optional[str] = Field(
        default=None,
        description="Optional cron expression.",
    )
    repeat_count: Optional[int] = Field(default=1, description="Optional repeat count. `null` means infinite.")
    next_fire_at: Optional[str] = Field(default=None, description="Optional next-fire timestamp.")
    created_at: Optional[str] = Field(default=None, description="Optional created-at timestamp.")
    enabled: Optional[bool] = Field(default=True, description="Optional enabled flag.")

    def to_scheduled_message(self) -> ScheduledMessage:
        schedule_kwargs = self.model_dump(exclude_none=True)
        schedule_kwargs["payload"] = self.payload.to_task()
        schedule_kwargs["management_class"] = ScheduleManagementClass.USER
        return ScheduledMessage(**schedule_kwargs)


def _turn_decision_force_prompt(
    tool_name: str, *, is_team_lead: bool, compatible: bool = False
) -> str:
    """Keep recovery instructions aligned with the selected role and schema."""
    credential_guidance = (
        '- To show a broker-owned credential, signup, manual-signup, or credential\n'
        '  deletion form, use `action_type="secure_credential_collection_request"` with\n'
        '  `actions[].secure_credential_collection_request`.\n'
        '- For approved signup automation, use `action_type="signup_automation_request"`\n'
        '  with `actions[].signup_automation_request`.'
        if is_team_lead
        else '- If you need a credential, sign-in, account, signup, or credential deletion,\n'
        '  use `action_type="manager_escalation"` with a task for TeamArchitect and\n'
        '  `actions[].escalation.escalation_type="needs_user_input"`. Describe what\n'
        '  setup is needed without secrets, then wait for TeamArchitect\'s reply.'
    )
    prompt = f"""
Call the `{tool_name}` structured-output tool now.
Do not answer in prose, Markdown, JSON text, or any other top-level text.
Choose exactly the action that matches the turn:
- For a normal answer to the current request, use `action_type="task_response"`
  and put the user-visible answer in `actions[].task.answer`.
- If you wrote, updated, saved, or generated a user-requested file or artifact,
  still finish with `action_type="task_response"` and summarize the file path,
  what changed, and any verification in `actions[].task.answer`.
- If this turn called `notify_user`, its tool result identifies text already
  sent to the user. In a later `task_response`, do not repeat that text
  verbatim; continue with only new details, caveats, or next steps.
- For a top-level timer/scheduled check that completed successfully but should
  not send anything to the user because nothing changed, use
  `action_type="task_response"`, set `suppress_outbound_dispatch=true`, and put
  a concise internal completion note in `actions[].task.answer`. Never set
  `suppress_outbound_dispatch=true` for delegated subtasks, direct user
  requests, or explicitly requested follow-up work; those turns must return a
  normal `task_response` to their requester. A recurring check sharing earlier
  conversation history is not, by itself, a follow-up request. Do not use bare `noop` to finish an accepted task.
- To ask the user for missing information, approval, or a blocking
  clarification, use `action_type="task_query"` with `query_type="request"` and
  put the question in `actions[].task_query.message`. If there are stable
  choices, include `choice_mode` (`single` or `multiple`) and `choices`.
- To answer an inbound TaskQuery, use `action_type="task_query"` with
  `query_type="response"` and put the answer in `actions[].task_query.message`.
- For delegated work, use `action_type="delegate_task"` with
  `actions[].task.problem_statement` and the exact specialist target on the action.
- If you are a specialist or worker and need user input, another specialist, or
  manager handling for a worker-origin scheduled result, use
  `action_type="manager_escalation"` with a task for TeamArchitect and an
  `escalation` payload. Then wait for TeamArchitect's reply instead of
  contacting the user or another specialist directly.
- To create a schedule, use `action_type="schedule_message"` with
  `actions[].scheduled_message`, followed by `task_response` to confirm it,
  or an action that waits for a result. A schedule cannot finish a task turn.
{credential_guidance}
- If no outbound effect is needed outside an accepted task turn, use
  `action_type="noop"` with a concise reason.
If you already drafted prose or Markdown, put that text inside the chosen action
payload instead of sending it as top-level text.
Omit runtime-owned identifiers such as `task_id`, `parent_task_id`,
`task_query_id`, message ids, routing paths, task state, and execution history;
omit `requester_type` as well. The runtime fills reply linkage, requester
identity, and transport metadata.
""".strip()
    if compatible:
        label = "Fireworks compatibility" if tool_name == "FireworksModelAgentTurnDecision" else "compatibility"
        prompt += (
            f"\nThis {label} tool uses a simpler provider-facing schema, "
            "but the runtime still validates the returned action batch through the "
            "canonical ModelAgentTurnDecision contract before executing any action. "
            "For task_response actions, put the user-visible answer in "
            "`actions[].task.answer`; never use `actions[].task.response`."
        )
    return prompt


MODEL_AGENT_TURN_DECISION_FORCE_PROMPT = _turn_decision_force_prompt(
    "ModelAgentTurnDecision", is_team_lead=True
)
MODEL_WORKER_TURN_DECISION_FORCE_PROMPT = _turn_decision_force_prompt(
    "ModelWorkerTurnDecision", is_team_lead=False
)
FIREWORKS_MODEL_AGENT_TURN_DECISION_FORCE_PROMPT = _turn_decision_force_prompt(
    "FireworksModelAgentTurnDecision", is_team_lead=True, compatible=True
)
COMPATIBLE_MODEL_AGENT_TURN_DECISION_FORCE_PROMPT = _turn_decision_force_prompt(
    "CompatibleModelAgentTurnDecision", is_team_lead=True, compatible=True
)
COMPATIBLE_WORKER_TURN_DECISION_FORCE_PROMPT = _turn_decision_force_prompt(
    "CompatibleWorkerTurnDecision", is_team_lead=False, compatible=True
)


def _require_agent_turn_decision_selector(schema: Dict[str, Any]) -> None:
    """Keep the compatibility selector required and provider-constrained."""

    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return
    selector = properties.get("selected_message_type")
    if not isinstance(selector, dict):
        return
    selector.pop("default", None)
    selector["type"] = "string"
    selector["enum"] = ["AgentTurnDecision"]
    required = schema.setdefault("required", [])
    if "selected_message_type" not in required:
        required.append("selected_message_type")


class ModelTaskRequestPayload(BaseModel):
    """Slim model-facing task request payload."""

    model_config = ConfigDict(extra="forbid")

    problem_statement: str = Field(description="Required task instructions for the delegated worker.")
    task_plan: Optional[str] = Field(default=None, description="Optional high-level execution plan.")
    thoughts_info: Optional[Union[List[Any], str]] = Field(
        default=None,
        description="Optional concise notes relevant to the task.",
    )

    def to_structured_task_payload(self) -> StructuredTaskPayload:
        return StructuredTaskPayload(
            task_type=MessageMetaType.REQUEST,
            problem_statement=self.problem_statement,
            task_plan=self.task_plan,
            thoughts_info=self.thoughts_info,
        )


class ModelTaskResponsePayload(BaseModel):
    """Slim model-facing task response payload."""

    model_config = ConfigDict(extra="forbid")

    answer: Union[TaskAnswer, str] = Field(description="Required user-visible answer text or TaskAnswer.")
    errors: Optional[List[str]] = Field(default=None, description="Optional error list.")
    task_plan: Optional[str] = Field(default=None, description="Optional high-level plan summary.")
    thoughts_info: Optional[Union[List[Any], str]] = Field(
        default=None,
        description="Optional concise notes relevant to the response.",
    )

    def to_structured_task_payload(self) -> StructuredTaskPayload:
        return StructuredTaskPayload(
            task_type=MessageMetaType.RESPONSE,
            answer=self.answer,
            errors=self.errors,
            task_plan=self.task_plan,
            thoughts_info=self.thoughts_info,
        )


class ModelTaskQueryPayload(BaseModel):
    """Slim model-facing TaskQuery payload."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    query_type: MessageMetaType = Field(description="Required. `request` or `response`.")
    message: str = Field(
        min_length=1,
        description="Required non-empty clarification request or clarification response text.",
    )
    query_reason: Optional[TaskQueryReason] = Field(
        default=None,
        description="Optional machine-readable query reason.",
    )
    allowed_decisions: Optional[List[TaskQueryDecision]] = Field(
        default=None,
        description="Optional allowed structured decisions for recoverable pauses.",
    )
    recoverable_pause_trigger: Optional[RecoverablePauseTrigger] = Field(
        default=None,
        description="Optional recoverable runtime pause trigger.",
    )
    choice_mode: Optional[Literal["single", "multiple"]] = Field(
        default=None,
        description="Optional first-class choice UI mode.",
    )
    choices: Optional[List[TaskQueryChoiceOption]] = Field(
        default=None,
        description="Optional first-class choices to render to the requester.",
    )
    allow_free_text: Optional[bool] = Field(
        default=None,
        description="Whether free-form text responses are allowed in addition to choices.",
    )
    selected_choice_ids: Optional[List[str]] = Field(
        default=None,
        description="Selected option ids when this is a TaskQuery response.",
    )

    def to_task_query(self) -> TaskQuery:
        query_kwargs = self.model_dump(exclude_none=True)
        return TaskQuery(**query_kwargs)


class ModelScheduledMessagePayload(BaseModel):
    """Slim model-facing ScheduledMessage payload."""

    model_config = ConfigDict(extra="forbid")

    schedule_id: Optional[str] = Field(default=None, description="Optional schedule id.")
    payload: ModelTaskRequestPayload = Field(description="Required task request to run when the schedule fires.")
    interval_ms: Optional[int] = Field(
        default=None,
        description="Optional fixed repeat interval in milliseconds.",
    )
    cron_expression: Optional[str] = Field(
        default=None,
        description="Optional cron expression.",
    )
    repeat_count: Optional[int] = Field(default=1, description="Optional repeat count. `null` means infinite.")
    next_fire_at: Optional[str] = Field(default=None, description="Optional next-fire timestamp.")
    created_at: Optional[str] = Field(default=None, description="Optional created-at timestamp.")
    enabled: Optional[bool] = Field(default=True, description="Optional enabled flag.")

    def to_structured_scheduled_message_payload(self) -> StructuredScheduledMessagePayload:
        return StructuredScheduledMessagePayload(
            schedule_id=self.schedule_id,
            payload=self.payload.to_structured_task_payload(),
            interval_ms=self.interval_ms,
            cron_expression=self.cron_expression,
            repeat_count=self.repeat_count,
            next_fire_at=self.next_fire_at,
            created_at=self.created_at,
            enabled=self.enabled,
        )


class StructuredLLMResponse(BaseModel):
    """
    Direct output contract for structured replies.

    Instructions:
    - Never submit an empty object. `{}` is invalid.
    - Set `selected_message_type`.
    - Fill exactly one matching payload field.
    - Leave every other payload field empty.
    - If `selected_message_type` is `Task`, you MUST populate the top-level
      `task` object. Put `task_type`, `task_id`, `problem_statement`, `answer`,
      and other Task fields inside `task`; never place them beside
      `selected_message_type`.
    - For delegated Task requests, routing metadata is a sibling of `task`.
      Set `target_named_agent_type` or `target_agent_name` at this
      `StructuredLLMResponse` top level, never inside the `task` object.
    - For replies, focus on the payload; the runtime handles correlation and final routing.
    - Do not emit `AgentCreationRequest` here. Agent creation is a tool-side action in
      the current runtime (`instantiate_agent(...)` / `instantiate_team_template(...)`).
    """

    selected_message_type: Literal[
        "AgentStatus",
        "ControlInstruction",
        "NoOpMessage",
        "ScheduledMessage",
        "SecureCredentialCollectionRequest",
        "SecureCredentialCollectionResult",
        "SignupAutomationRequest",
        "SignupAutomationResult",
        "SystemStatusMessage",
        "Task",
        "TaskQuery",
    ] = Field(
        description=(
            "Always required. Never call StructuredLLMResponse with an empty object. Pick the outgoing "
            "message type. After choosing it, populate exactly one matching payload field with the same "
            "semantic name, e.g. choose `Task` and populate the top-level `task` object."
        )
    )
    agent_status: Optional[AgentStatus] = Field(
        default=None,
        description="Set this only when `selected_message_type` is `AgentStatus`.",
    )
    control_instruction: Optional[ControlInstruction] = Field(
        default=None,
        description="Set this only when `selected_message_type` is `ControlInstruction`.",
    )
    no_op_message: Optional[NoOpMessage] = Field(
        default=None,
        description="Set this only when `selected_message_type` is `NoOpMessage`.",
    )
    scheduled_message: Optional[StructuredScheduledMessagePayload] = Field(
        default=None,
        description="Set this only when `selected_message_type` is `ScheduledMessage`.",
    )
    secure_credential_collection_request: Optional[SecureCredentialCollectionRequest] = Field(
        default=None,
        description=(
            "Set this only when `selected_message_type` is `SecureCredentialCollectionRequest`. "
            "Use this when a task needs named credentials or approved signup/manual fallback. "
            "The gateway creates and sends the broker form URL only for this structured request; "
            "do not return a normal Task response claiming a form is ready. Never include raw "
            "passwords, tokens, generated secrets, TOTP seeds, or credential files."
        ),
    )
    secure_credential_collection_result: Optional[SecureCredentialCollectionResult] = Field(
        default=None,
        description="Set this only when `selected_message_type` is `SecureCredentialCollectionResult`.",
    )
    signup_automation_request: Optional[SignupAutomationRequest] = Field(
        default=None,
        description=(
            "Set this only when `selected_message_type` is `SignupAutomationRequest`. "
            "Only use after explicit user approval; secret fields are broker-owned."
        ),
    )
    signup_automation_result: Optional[SignupAutomationResult] = Field(
        default=None,
        description="Set this only when `selected_message_type` is `SignupAutomationResult`.",
    )
    system_status_message: Optional[SystemStatusMessage] = Field(
        default=None,
        description="Set this only when `selected_message_type` is `SystemStatusMessage`.",
    )
    task: Optional[StructuredTaskPayload] = Field(
        default=None,
        description=(
            "Required when `selected_message_type` is `Task`. This must be a nested object containing "
            "Task fields such as `task_type`, `task_id`, `problem_statement`, and `answer`. Do not put "
            "`task_type`, `task_id`, or `answer` at the StructuredLLMResponse top level. Do not put "
            "`target_named_agent_type`, `target_agent_name`, `is_response_requested`, `requester_type`, "
            "or `msg_meta` inside `task`; those routing fields belong at the StructuredLLMResponse top level."
        ),
    )
    task_query: Optional[TaskQuery] = Field(
        default=None,
        description="Set this only when `selected_message_type` is `TaskQuery`.",
    )
    after_action_report: Optional[AfterActionReport] = Field(
        default=None,
        description=(
            "Optional non-blocking follow-up report. Only set this with a terminal "
            "`Task` response when durable, non-obvious context should be sent to the manager after the answer."
        ),
    )
    target_named_agent_type: Optional[str] = Field(
        default=None,
        description=(
            "Optional top-level sibling of `task`. For a new request, name the specialist queue to handle it, "
            "for example `CodeWritingAgent`. Do not place this field inside `task`."
        ),
    )
    target_agent_name: Optional[str] = Field(
        default=None,
        description=(
            "Optional top-level sibling of `task`. For a new request, target one specific agent by name. "
            "Do not place this field inside `task`."
        ),
    )
    requester_type: Optional[RequesterType] = Field(
        default=None,
        description="Optional semantic requester type for the outgoing message.",
    )
    is_response_requested: Optional[bool] = Field(
        default=None,
        description=(
            "Optional top-level sibling of `task`. Set `true` when this new request expects a reply. "
            "Leave empty for final responses. Do not place this field inside `task`."
        ),
    )
    msg_meta: Optional[MessageMeta] = Field(
        default=None,
        description="Optional metadata. Prefer the flat routing fields above for task delegation.",
    )

    @classmethod
    def _payload_field_by_type(cls) -> Dict[str, str]:
        return {
            "AgentStatus": "agent_status",
            "ControlInstruction": "control_instruction",
            "NoOpMessage": "no_op_message",
            "ScheduledMessage": "scheduled_message",
            "SecureCredentialCollectionRequest": "secure_credential_collection_request",
            "SecureCredentialCollectionResult": "secure_credential_collection_result",
            "SignupAutomationRequest": "signup_automation_request",
            "SignupAutomationResult": "signup_automation_result",
            "SystemStatusMessage": "system_status_message",
            "Task": "task",
            "TaskQuery": "task_query",
        }

    @model_validator(mode="after")
    def validate_selected_payload(self) -> "StructuredLLMResponse":
        payload_map = self._payload_field_by_type()
        expected_field = payload_map[self.selected_message_type]
        populated_fields = [field_name for field_name in payload_map.values() if getattr(self, field_name) is not None]

        if getattr(self, expected_field) is None:
            raise ValueError(
                f"{expected_field} must be populated when selected_message_type is {self.selected_message_type}."
            )

        extra_fields = [field_name for field_name in populated_fields if field_name != expected_field]
        if extra_fields:
            raise ValueError(
                f"Only {expected_field} may be populated when selected_message_type is {self.selected_message_type}; "
                f"found extra payload fields: {', '.join(extra_fields)}."
            )

        if self.after_action_report is not None:
            if self.selected_message_type != "Task" or self.task is None or self.task.task_type != MessageMetaType.RESPONSE:
                raise ValueError("after_action_report may only accompany a terminal Task response.")

        return self

    @model_validator(mode="before")
    @classmethod
    def normalize_misnested_task_routing(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        if data.get("selected_message_type") != "Task":
            return data
        task_payload = data.get("task")
        if not isinstance(task_payload, dict):
            return data

        normalized = dict(data)
        normalized_task = dict(task_payload)
        nested_meta = normalized_task.get("msg_meta")

        def nested_meta_value(field_name: str) -> Any:
            if isinstance(nested_meta, dict):
                return nested_meta.get(field_name)
            return getattr(nested_meta, field_name, None)

        promotions = {
            "target_named_agent_type": cls._first_present(
                normalized_task.get("target_named_agent_type"),
                normalized_task.get("to_named_agent_type"),
                nested_meta_value("to_named_agent_type"),
            ),
            "target_agent_name": cls._first_present(
                normalized_task.get("target_agent_name"),
                normalized_task.get("to_agent_name"),
                nested_meta_value("to_agent_name"),
            ),
            "requester_type": cls._first_present(
                normalized_task.get("requester_type"),
                nested_meta_value("requester_type"),
            ),
            "is_response_requested": cls._first_present(
                normalized_task.get("is_response_requested"),
                nested_meta_value("is_response_requested"),
            ),
        }

        for field_name, nested_value in promotions.items():
            normalized[field_name] = cls._reconcile_field(
                field_name,
                normalized.get(field_name),
                nested_value,
            )
        if normalized.get("msg_meta") is None and nested_meta is not None:
            normalized["msg_meta"] = nested_meta

        for field_name in (
            "target_named_agent_type",
            "target_agent_name",
            "to_named_agent_type",
            "to_agent_name",
            "requester_type",
            "is_response_requested",
            "msg_meta",
        ):
            normalized_task.pop(field_name, None)
        normalized["task"] = normalized_task
        return normalized

    @staticmethod
    def _first_present(*values: Any) -> Any:
        for value in values:
            if value is not None:
                return value
        return None

    @staticmethod
    def _reconcile_field(field_name: str, outer_value: Any, nested_value: Any) -> Any:
        if nested_value is None:
            return outer_value
        if outer_value is None:
            return nested_value
        if outer_value != nested_value:
            raise ValueError(
                f"Conflicting routing metadata for {field_name}: top-level value {outer_value!r} "
                f"does not match nested task value {nested_value!r}."
            )
        return outer_value

    def to_comms_message(self) -> CommsMessage:
        payload_field = self._payload_field_by_type()[self.selected_message_type]
        payload = getattr(self, payload_field)
        if isinstance(payload, StructuredTaskPayload):
            payload = payload.to_task()
        elif isinstance(payload, StructuredScheduledMessagePayload):
            payload = payload.to_scheduled_message()
        synthesized_meta = self.msg_meta.model_copy(deep=True) if self.msg_meta else MessageMeta()
        if self.target_named_agent_type is not None:
            synthesized_meta.to_named_agent_type = self.target_named_agent_type
        if self.target_agent_name is not None:
            synthesized_meta.to_agent_name = self.target_agent_name
        if self.requester_type is not None:
            synthesized_meta.requester_type = self.requester_type
        if self.is_response_requested is not None:
            synthesized_meta.is_response_requested = self.is_response_requested

        return CommsMessage(
            msg_content=payload,
            msg_meta=(
                synthesized_meta
                if synthesized_meta.model_dump(exclude_none=True, exclude_defaults=True)
                else None
            ),
        )


class ModelDelegateTaskAction(BaseModel):
    """Slim model-facing runtime action for delegated Task requests."""

    model_config = ConfigDict(extra="forbid")

    action_type: Literal["delegate_task"] = "delegate_task"
    task: ModelTaskRequestPayload = Field(description="Task request payload to delegate.")
    target_named_agent_type: Optional[str] = Field(default=None, description="Named specialist queue to receive the task.")
    target_agent_name: Optional[str] = Field(default=None, description="Specific agent name to receive the task.")
    is_response_requested: bool = Field(default=True, description="Whether the delegated task expects a response.")
    check_back_after_minutes: Optional[float] = Field(
        default=None,
        ge=1,
        le=10080,
        description="Optional. Minutes after which you want to look at this request's delegated tasks again even if not all are done (for example to answer with partial results or stop slow ones). Without it you are woken when every delegated task has ended, or earlier if one fails after automatic retries.",
    )
    manager_escalation: Optional[ManagerEscalationContext] = Field(
        default=None,
        description="Optional worker-origin metadata when delegating to TeamArchitect.",
    )

    def to_runtime_action(self) -> "DelegateTaskAction":
        target_named_agent_type = self.target_named_agent_type
        if self.manager_escalation is not None and not target_named_agent_type and not self.target_agent_name:
            target_named_agent_type = TEAM_ARCHITECT_AGENT_TYPE
        return DelegateTaskAction(
            task=self.task.to_structured_task_payload(),
            target_named_agent_type=target_named_agent_type,
            target_agent_name=self.target_agent_name,
            is_response_requested=self.is_response_requested,
            check_back_after_minutes=self.check_back_after_minutes,
            manager_escalation=self.manager_escalation,
        )


class ModelManagerEscalationAction(BaseModel):
    """Worker-facing shortcut for manager-mediated escalation."""

    model_config = ConfigDict(extra="forbid")

    action_type: Literal["manager_escalation"] = "manager_escalation"
    task: ModelTaskRequestPayload = Field(
        description="Task request for TeamArchitect describing the required decision, help, or worker result."
    )
    escalation: ManagerEscalationContext = Field(description="Worker-origin escalation metadata.")

    def to_runtime_action(self) -> "DelegateTaskAction":
        return DelegateTaskAction(
            task=self.task.to_structured_task_payload(),
            target_named_agent_type=TEAM_ARCHITECT_AGENT_TYPE,
            is_response_requested=True,
            requester_type=RequesterType.AGENT_REQUESTER,
            manager_escalation=self.escalation,
        )


class ModelTaskResponseAction(BaseModel):
    """Slim model-facing runtime action for terminal Task responses."""

    model_config = ConfigDict(extra="forbid")

    action_type: Literal["task_response"] = "task_response"
    task: ModelTaskResponsePayload = Field(description="Task response payload.")
    suppress_outbound_dispatch: bool = Field(
        default=False,
        description=(
            "Set true only for top-level timer/scheduled task checks that completed successfully "
            "and require no user-visible message. Never set true for delegated subtasks, direct "
            "user requests, or follow-up work. The runtime completes valid silent timer tasks "
            "locally and suppresses gateway/user delivery."
        ),
    )
    after_action_report: Optional[AfterActionReport] = None

    @model_validator(mode="before")
    @classmethod
    def normalize_flat_response_payload(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value

        task_value = value.get("task")
        if isinstance(task_value, dict):
            return value
        if task_value is not None and not isinstance(task_value, str):
            return value

        normalized = dict(value)
        task_payload: dict[str, Any] = {}
        if isinstance(task_value, str):
            task_payload["answer"] = task_value
            normalized.pop("task", None)
        elif "task" in normalized:
            normalized.pop("task", None)

        for field_name in ("answer", "errors", "task_plan", "thoughts_info"):
            if field_name in normalized:
                task_payload[field_name] = normalized.pop(field_name)

        if task_payload:
            normalized["task"] = task_payload
        return normalized

    def to_runtime_action(self) -> "TaskResponseAction":
        return TaskResponseAction(
            task=self.task.to_structured_task_payload(),
            suppress_outbound_dispatch=self.suppress_outbound_dispatch,
            after_action_report=self.after_action_report,
        )


class ModelTaskQueryAction(BaseModel):
    """Slim model-facing runtime action for TaskQuery traffic."""

    model_config = ConfigDict(extra="forbid")

    action_type: Literal["task_query"] = "task_query"
    task_query: ModelTaskQueryPayload

    def to_runtime_action(self) -> "TaskQueryAction":
        return TaskQueryAction(task_query=self.task_query.to_task_query())


class ModelScheduleMessageAction(BaseModel):
    """Slim model-facing runtime action for persisting a ScheduledMessage."""

    model_config = ConfigDict(extra="forbid")

    action_type: Literal["schedule_message"] = "schedule_message"
    scheduled_message: ModelScheduledMessagePayload

    def to_runtime_action(self) -> "ScheduleMessageAction":
        return ScheduleMessageAction(
            scheduled_message=self.scheduled_message.to_structured_scheduled_message_payload()
        )


class ModelSecureCredentialCollectionAction(BaseModel):
    """Model-facing action for gateway-assisted credential collection or deletion forms."""

    model_config = ConfigDict(extra="forbid")

    action_type: Literal["secure_credential_collection_request"] = "secure_credential_collection_request"
    secure_credential_collection_request: SecureCredentialCollectionRequest = Field(
        description=(
            "Secure credential request to send to the gateway. Use flow='credential_deletion' "
            "for user-confirmed broker deletion forms."
        )
    )

    def to_runtime_action(self) -> "SecureCredentialCollectionAction":
        return SecureCredentialCollectionAction(
            secure_credential_collection_request=self.secure_credential_collection_request
        )


class ModelSignupAutomationAction(BaseModel):
    """Model-facing action for approved signup automation or manual fallback."""

    model_config = ConfigDict(extra="forbid")

    action_type: Literal["signup_automation_request"] = "signup_automation_request"
    signup_automation_request: SignupAutomationRequest

    def to_runtime_action(self) -> "SignupAutomationAction":
        return SignupAutomationAction(signup_automation_request=self.signup_automation_request)


class ModelNoOpAction(BaseModel):
    """Model-facing action that records no outbound effect is required."""

    model_config = ConfigDict(extra="forbid")

    action_type: Literal["noop"] = "noop"
    reason: str = Field(default="No runtime action requested.")

    def to_runtime_action(self) -> "NoOpAction":
        return NoOpAction(reason=self.reason)


ModelTurnAction = Annotated[
    Union[
        ModelDelegateTaskAction,
        ModelManagerEscalationAction,
        ModelTaskResponseAction,
        ModelTaskQueryAction,
        ModelScheduleMessageAction,
        ModelSecureCredentialCollectionAction,
        ModelSignupAutomationAction,
        ModelNoOpAction,
    ],
    Field(discriminator="action_type"),
]


class ModelAgentTurnDecision(BaseModel):
    """Slim model-facing structured-output contract for one ordered runtime action batch."""

    model_config = ConfigDict(extra="forbid")

    selected_message_type: Literal["AgentTurnDecision"] = Field(
        default="AgentTurnDecision",
        description="Always `AgentTurnDecision` for runtime action decisions.",
    )
    actions: List[ModelTurnAction] = Field(
        min_length=1,
        description="Ordered runtime actions. Each action completes before the next action begins.",
    )
    summary_for_history: Optional[str] = Field(
        default=None,
        description="Optional concise runtime-visible summary of why these actions were selected.",
    )

    @model_validator(mode="after")
    def validate_action_order(self) -> "ModelAgentTurnDecision":
        _validate_action_batch_end(self.actions)
        terminal_response_indexes = [
            index for index, action in enumerate(self.actions)
            if isinstance(action, ModelTaskResponseAction)
        ]
        if terminal_response_indexes and terminal_response_indexes[-1] != len(self.actions) - 1:
            raise ValueError("task_response must be the final action in the ordered batch.")
        if len(terminal_response_indexes) > 1:
            raise ValueError("Only one task_response action is allowed in a batch.")
        return self

    def to_runtime_decision(self) -> "StructuredAgentTurnDecision":
        return StructuredAgentTurnDecision(
            actions=[action.to_runtime_action() for action in self.actions],
            summary_for_history=self.summary_for_history,
        )


# Only TeamArchitect may ask the person for anything (ADR-018), so specialists
# get a turn answer without the credential-form and signup actions: a worker
# that needs a credential, sign-in or account escalates with
# ``manager_escalation`` (needs_user_input) and TeamArchitect sets it up with
# ``set_up_tools`` or its credential form. Before 2026-10-07 workers had these
# actions; their requests went to the delegating agent, which had no handler and
# dropped them, and the worker waited until the watchdog failed its task.
WORKER_EXCLUDED_ACTION_TYPES = frozenset(
    {"secure_credential_collection_request", "signup_automation_request"}
)

ModelWorkerTurnAction = Annotated[
    Union[
        ModelDelegateTaskAction,
        ModelManagerEscalationAction,
        ModelTaskResponseAction,
        ModelTaskQueryAction,
        ModelScheduleMessageAction,
        ModelNoOpAction,
    ],
    Field(discriminator="action_type"),
]


class ModelWorkerTurnDecision(ModelAgentTurnDecision):
    """``ModelAgentTurnDecision`` for specialists: no credential-form or signup actions."""

    actions: List[ModelWorkerTurnAction] = Field(  # type: ignore[assignment]
        min_length=1,
        description="Ordered runtime actions. Each action completes before the next action begins.",
    )


class FireworksModelTaskPayload(BaseModel):
    """Simple Fireworks-facing task payload without unions or discriminators."""

    model_config = ConfigDict(extra="forbid")

    problem_statement: Optional[str] = Field(default=None, description="Required for delegated task requests.")
    answer: Optional[str] = Field(default=None, description="Required user-visible answer text for task responses.")
    errors: Optional[List[str]] = Field(default=None, description="Optional response error list.")
    task_plan: Optional[str] = Field(default=None, description="Optional high-level plan summary.")
    thoughts_info: Optional[str] = Field(default=None, description="Optional concise notes relevant to the task.")


class FireworksModelTaskQueryPayload(BaseModel):
    """Simple Fireworks-facing TaskQuery payload with required semantic fields."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    query_type: Literal["request", "response"] = Field(description="Required. `request` or `response`.")
    message: str = Field(description="Required non-empty clarification request or clarification response text.")
    query_reason: Optional[TaskQueryReason] = Field(default=None, description="Optional machine-readable query reason.")
    allowed_decisions: Optional[List[TaskQueryDecision]] = Field(
        default=None,
        description="Optional allowed structured decisions for recoverable pauses.",
    )
    recoverable_pause_trigger: Optional[RecoverablePauseTrigger] = Field(
        default=None,
        description="Optional recoverable runtime pause trigger.",
    )
    choice_mode: Optional[Literal["single", "multiple"]] = Field(
        default=None,
        description="Optional first-class choice UI mode.",
    )
    choices: Optional[List[TaskQueryChoiceOption]] = Field(
        default=None,
        description="Optional first-class choices to render to the requester.",
    )
    allow_free_text: Optional[bool] = Field(
        default=None,
        description="Whether free-form text responses are allowed in addition to choices.",
    )
    selected_choice_ids: Optional[List[str]] = Field(
        default=None,
        description="Selected option ids when this is a TaskQuery response.",
    )

    @model_validator(mode="after")
    def validate_message(self) -> "FireworksModelTaskQueryPayload":
        if not self.message:
            raise ValueError("task_query.message must be non-empty.")
        return self


class CompatibleModelTaskPayload(FireworksModelTaskPayload):
    """Provider-compatible task payload without unions or discriminators."""


class CompatibleModelTaskQueryPayload(FireworksModelTaskQueryPayload):
    """Provider-compatible TaskQuery payload with required semantic fields."""


CompatibleTurnActionType = Literal[
    "delegate_task",
    "manager_escalation",
    "task_response",
    "task_query",
    "schedule_message",
    "secure_credential_collection_request",
    "signup_automation_request",
    "noop",
]


class FireworksModelTurnAction(BaseModel):
    """Fireworks-compatible schema wrapper for one model-facing runtime action.

    Fireworks rejects `oneOf` and also rejects schema properties that lose their
    `type` during Strands' schema flattening. This wrapper keeps the provider
    schema simple, then validates the returned payload through
    `ModelAgentTurnDecision` before the runtime executes anything.
    """

    model_config = ConfigDict(extra="forbid")

    action_type: CompatibleTurnActionType = Field(
        description=(
            "Required action type: delegate_task, manager_escalation, task_response, "
            "task_query, schedule_message, secure_credential_collection_request, "
            "signup_automation_request, or noop."
        )
    )
    task: Optional[FireworksModelTaskPayload] = Field(default=None, description="Task request or response payload.")
    target_named_agent_type: Optional[str] = Field(default=None, description="Named specialist queue for delegation.")
    target_agent_name: Optional[str] = Field(default=None, description="Specific agent name for delegation.")
    is_response_requested: Optional[bool] = Field(default=None, description="Whether delegated work expects a reply.")
    check_back_after_minutes: Optional[float] = Field(
        default=None,
        description="Optional minutes after which to look at delegated work again before all of it ends.",
    )
    manager_escalation: Optional[Dict[str, Any]] = Field(default=None, description="Optional manager escalation metadata.")
    escalation: Optional[Dict[str, Any]] = Field(default=None, description="Manager escalation payload.")
    suppress_outbound_dispatch: Optional[bool] = Field(default=None, description="Suppress eligible timer reply delivery.")
    after_action_report: Optional[Dict[str, Any]] = Field(default=None, description="Optional follow-up report payload.")
    task_query: Optional[FireworksModelTaskQueryPayload] = Field(
        default=None,
        description=(
            "Required when action_type is task_query. Must contain query_type (`request` or `response`) "
            "and a non-empty message. Never provide an empty object."
        ),
    )
    scheduled_message: Optional[Dict[str, Any]] = Field(default=None, description="ScheduledMessage payload.")
    secure_credential_collection_request: Optional[SecureCredentialCollectionRequest] = Field(
        default=None,
        description="Secure credential collection or deletion request payload.",
    )
    signup_automation_request: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Signup automation request payload.",
    )
    reason: Optional[str] = Field(default=None, description="Reason for noop actions. Omit for all other action types.")

    @model_validator(mode="after")
    def validate_action_payload(self) -> "FireworksModelTurnAction":
        required_payload_fields_by_action_type = {
            "delegate_task": ("task",),
            "manager_escalation": ("task", "escalation"),
            "task_response": ("task",),
            "task_query": ("task_query",),
            "schedule_message": ("scheduled_message",),
            "secure_credential_collection_request": ("secure_credential_collection_request",),
            "signup_automation_request": ("signup_automation_request",),
        }
        required_fields = required_payload_fields_by_action_type.get(self.action_type)
        if required_fields is None:
            return self

        missing_fields = [field_name for field_name in required_fields if getattr(self, field_name) is None]
        if missing_fields:
            raise ValueError(
                f"{', '.join(missing_fields)} required when action_type is {self.action_type}."
            )
        if self.reason is not None and self.action_type != "noop":
            raise ValueError("reason may only be set when action_type is noop.")
        return self


class CompatibleModelTurnAction(FireworksModelTurnAction):
    """Provider-compatible schema wrapper for one model-facing runtime action.

    This keeps action selection as a simple required string instead of exposing
    a discriminated union to providers that reject or follow that schema poorly.
    Runtime validation still passes through the canonical `ModelAgentTurnDecision`.
    """

    task: Optional[CompatibleModelTaskPayload] = Field(default=None, description="Task request or response payload.")
    task_query: Optional[CompatibleModelTaskQueryPayload] = Field(
        default=None,
        description=(
            "Required when action_type is task_query. Must contain query_type (`request` or `response`) "
            "and a non-empty message. Never provide an empty object."
        ),
    )


class FireworksModelAgentTurnDecision(BaseModel):
    """Fireworks-compatible wrapper for `ModelAgentTurnDecision`.

    This is not a relaxed transport contract. It only avoids provider-unsupported
    JSON Schema constructs; the returned object is immediately validated through
    the canonical model-facing turn decision.
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra=_require_agent_turn_decision_selector,
    )

    selected_message_type: str = Field(
        default="AgentTurnDecision",
        description="Always `AgentTurnDecision` for runtime action decisions.",
    )
    actions: List[FireworksModelTurnAction] = Field(
        description="Ordered runtime actions. Each action completes before the next action begins.",
    )
    summary_for_history: Optional[str] = Field(
        default=None,
        description="Optional concise runtime-visible summary of why these actions were selected.",
    )

    @model_validator(mode="after")
    def validate_fireworks_wrapper(self) -> "FireworksModelAgentTurnDecision":
        if self.selected_message_type != "AgentTurnDecision":
            raise ValueError("selected_message_type must be AgentTurnDecision.")
        if not self.actions:
            raise ValueError("actions must contain at least one action.")
        try:
            ModelAgentTurnDecision.model_validate(self.model_dump(exclude_none=True))
        except ValidationError as exc:
            raise ValueError(
                "Action batch does not satisfy the canonical ModelAgentTurnDecision contract: "
                f"{exc}"
            ) from exc
        return self

    def to_model_agent_turn_decision(self) -> ModelAgentTurnDecision:
        return ModelAgentTurnDecision.model_validate(self.model_dump(exclude_none=True))

    def to_runtime_decision(self) -> "StructuredAgentTurnDecision":
        return self.to_model_agent_turn_decision().to_runtime_decision()


class CompatibleModelAgentTurnDecision(FireworksModelAgentTurnDecision):
    """Provider-compatible wrapper for `ModelAgentTurnDecision`.

    Some OpenAI-compatible routes accept the canonical schema but follow it
    poorly when action selection is represented as a discriminated union. This
    wrapper keeps the provider-facing action schema flat, then validates the
    returned object through `ModelAgentTurnDecision` before any runtime side
    effect executes.
    """

    actions: List[CompatibleModelTurnAction] = Field(
        description="Ordered runtime actions. Each action completes before the next action begins.",
    )


CompatibleWorkerTurnActionType = Literal[
    "delegate_task",
    "manager_escalation",
    "task_response",
    "task_query",
    "schedule_message",
    "noop",
]


class CompatibleWorkerTurnAction(CompatibleModelTurnAction):
    """``CompatibleModelTurnAction`` for specialists (see ``ModelWorkerTurnDecision``).

    The two payload fields stay on the class (the parent's validator reads
    them) but accept only ``None`` and are left out of the provider schema.
    """

    action_type: CompatibleWorkerTurnActionType = Field(  # type: ignore[assignment]
        description=(
            "Required action type: delegate_task, manager_escalation, task_response, "
            "task_query, schedule_message, or noop."
        )
    )
    secure_credential_collection_request: SkipJsonSchema[None] = None  # type: ignore[assignment]
    signup_automation_request: SkipJsonSchema[None] = None  # type: ignore[assignment]


class CompatibleWorkerTurnDecision(CompatibleModelAgentTurnDecision):
    """``CompatibleModelAgentTurnDecision`` for specialists."""

    actions: List[CompatibleWorkerTurnAction] = Field(  # type: ignore[assignment]
        description="Ordered runtime actions. Each action completes before the next action begins.",
    )


class DelegateTaskAction(BaseModel):
    """Runtime action for delegated Task requests."""

    action_type: Literal["delegate_task"] = "delegate_task"
    task: StructuredTaskPayload = Field(description="Task request payload to delegate.")
    target_named_agent_type: Optional[str] = Field(default=None, description="Named specialist queue to receive the task.")
    target_agent_name: Optional[str] = Field(default=None, description="Specific agent name to receive the task.")
    is_response_requested: bool = Field(default=True, description="Whether the delegated task expects a response.")
    requester_type: Optional[RequesterType] = Field(default=None, description="Optional requester type override.")
    check_back_after_minutes: Optional[float] = Field(
        default=None,
        ge=1,
        le=10080,
        description="Optional. Minutes after which you want to look at this request's delegated tasks again even if not all are done (for example to answer with partial results or stop slow ones). Without it you are woken when every delegated task has ended, or earlier if one fails after automatic retries.",
    )
    manager_escalation: Optional[ManagerEscalationContext] = Field(
        default=None,
        description="Optional worker-origin metadata for manager-mediated escalations.",
    )

    @model_validator(mode="after")
    def validate_delegate_task(self) -> "DelegateTaskAction":
        if self.task.task_type != MessageMetaType.REQUEST:
            raise ValueError("delegate_task requires task.task_type='request'.")
        if self.manager_escalation is not None and not self.target_named_agent_type and not self.target_agent_name:
            self.target_named_agent_type = TEAM_ARCHITECT_AGENT_TYPE
        return self


class TaskResponseAction(BaseModel):
    """Runtime action for a terminal Task response."""

    action_type: Literal["task_response"] = "task_response"
    task: StructuredTaskPayload = Field(description="Task response payload.")
    suppress_outbound_dispatch: bool = Field(
        default=False,
        description=(
            "True only for top-level timer-origin task responses that should complete locally "
            "without publishing a user-visible reply. Invalid use is repaired by clearing the "
            "flag and dispatching the response normally."
        ),
    )
    after_action_report: Optional[AfterActionReport] = None

    @model_validator(mode="after")
    def validate_task_response(self) -> "TaskResponseAction":
        if self.task.task_type != MessageMetaType.RESPONSE:
            raise ValueError("task_response requires task.task_type='response'.")
        return self


class TaskQueryAction(BaseModel):
    """Runtime action for TaskQuery traffic."""

    action_type: Literal["task_query"] = "task_query"
    task_query: TaskQuery


class ScheduleMessageAction(BaseModel):
    """Runtime action for persisting a ScheduledMessage."""

    action_type: Literal["schedule_message"] = "schedule_message"
    scheduled_message: StructuredScheduledMessagePayload


class SendMessageAction(BaseModel):
    """Runtime action for non-task protocol messages."""

    action_type: Literal["send_message"] = "send_message"
    message: StructuredLLMResponse

    @model_validator(mode="after")
    def validate_not_task_request(self) -> "SendMessageAction":
        comms_message = self.message.to_comms_message()
        payload = comms_message.msg_content
        if isinstance(payload, Task) and payload.task_type == MessageMetaType.REQUEST:
            raise ValueError("Task requests must use delegate_task, not send_message.")
        return self


class SecureCredentialCollectionAction(BaseModel):
    """Runtime action for gateway-assisted credential collection or deletion forms."""

    action_type: Literal["secure_credential_collection_request"] = "secure_credential_collection_request"
    secure_credential_collection_request: SecureCredentialCollectionRequest = Field(
        description=(
            "Secure credential request to send to the gateway. The gateway creates the broker form URL; "
            "do not wrap this in send_message. Use flow='credential_deletion' for deletion confirmation."
        )
    )


class SignupAutomationAction(BaseModel):
    """Runtime action for approved signup automation or manual fallback."""

    action_type: Literal["signup_automation_request"] = "signup_automation_request"
    signup_automation_request: SignupAutomationRequest


class NoOpAction(BaseModel):
    """Runtime action that records that no outbound effect is required."""

    action_type: Literal["noop"] = "noop"
    reason: str = Field(default="No runtime action requested.")


TurnAction = Annotated[
    Union[
        DelegateTaskAction,
        TaskResponseAction,
        TaskQueryAction,
        ScheduleMessageAction,
        SecureCredentialCollectionAction,
        SignupAutomationAction,
        SendMessageAction,
        NoOpAction,
    ],
    Field(discriminator="action_type"),
]


class StructuredAgentTurnDecision(BaseModel):
    """Structured-output contract for one ordered runtime action batch."""

    selected_message_type: Literal["AgentTurnDecision"] = Field(
        default="AgentTurnDecision",
        description="Always `AgentTurnDecision` for the runtime action outbox contract.",
    )
    actions: List[TurnAction] = Field(
        min_length=1,
        description="Ordered runtime actions. Each action completes before the next action begins.",
    )
    summary_for_history: Optional[str] = Field(
        default=None,
        description="Optional concise runtime-visible summary of why these actions were selected.",
    )

    @model_validator(mode="after")
    def validate_action_order(self) -> "StructuredAgentTurnDecision":
        _validate_action_batch_end(self.actions)
        terminal_response_indexes = [
            index for index, action in enumerate(self.actions)
            if isinstance(action, TaskResponseAction)
        ]
        if terminal_response_indexes and terminal_response_indexes[-1] != len(self.actions) - 1:
            raise ValueError("task_response must be the final action in the ordered batch.")
        if len(terminal_response_indexes) > 1:
            raise ValueError("Only one task_response action is allowed in a batch.")
        return self
