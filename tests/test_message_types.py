import json
import os
import sys

import pytest
from pydantic import ValidationError

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from custom_messages.contracts.control_v1 import AgentCreationRequest, AgentStatus, ControlInstruction, Instruction
from custom_messages.contracts.envelope_v1 import CommsMessage, MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.contracts.task_v1 import ExecutionPlanItem, ExecutionDigest, ExecutionUnknown, Task, TaskAnswer, TaskQuery, TaskQueryChoiceOption
from custom_messages.enums import ControlType, ExecutionPlanItemStatus, ExecutionUnknownStatus, MessageMetaType, RoutingType
from custom_messages.presentation.task_query import render_task_query_text
from custom_messages.schema import generate_all_schemas


class TestMessageSerialization:
    """Unit tests for serialization and deserialization of all content types."""

    def test_schema_generation(self):
        """Test the schema generation utility."""
        comms_schemas = generate_all_schemas()
        assert comms_schemas is not None
        assert "MessageEnvelope" in comms_schemas

        envelope_schema = generate_all_schemas(target_class=CommsMessage)
        assert envelope_schema is not None
        assert "CommsMessage" in envelope_schema

    def test_execution_digest_serialization(self):
        """Test ExecutionDigest round-trip serialization inside a Task."""
        digest = ExecutionDigest(
            goal_summary="Ship a durable execution tracker.",
            plan_summary="Add digest models, tools, and prompt rendering.",
            current_focus="TaskManager integration",
            plan_items=[
                ExecutionPlanItem(
                    step_id="plan_step_models",
                    title="Add digest models",
                    rationale="The plan must survive compaction as structured state.",
                    status=ExecutionPlanItemStatus.COMPLETED,
                    resolution_summary="ExecutionDigest now persists on Task.",
                )
            ],
            unknowns=[
                ExecutionUnknown(
                    unknown_id="unknown_prompt",
                    summary="How much of the digest should be rendered into prompts?",
                    why_it_matters="Too much duplication can waste context budget.",
                    resolution_plan="Render the digest once as a pinned section ahead of task history.",
                    status=ExecutionUnknownStatus.OPEN,
                )
            ],
        )
        task = Task(
            task_type=MessageMetaType.REQUEST,
            problem_statement="Persist execution digest",
            task_plan="Add a persistent execution digest",
            execution_digest=digest,
        )

        json_str = task.model_dump_json()
        restored = Task.model_validate_json(json_str)

        assert restored.execution_digest is not None
        assert restored.execution_digest.plan_summary == digest.plan_summary
        assert restored.execution_digest.plan_items[0].rationale == digest.plan_items[0].rationale
        assert restored.execution_digest.unknowns[0].summary == digest.unknowns[0].summary

    def test_task_query_serialization(self):
        """Test TaskQuery round-trip serialization."""
        query = TaskQuery(
            query_type=MessageMetaType.REQUEST,
            message="What version of python?",
            parent_task_id="task_123",
            choice_mode="single",
            choices=[
                TaskQueryChoiceOption(
                    option_id="python_313",
                    label="Python 3.13",
                    description="Use the project runtime.",
                    recommended=True,
                ),
                TaskQueryChoiceOption(option_id="python_312", label="Python 3.12"),
            ],
            allow_free_text=False,
        )
        assert query.message_class == "TaskQuery"
        assert query.get_id() == query.task_query_id

        json_str = query.model_dump_json()
        restored = TaskQuery.model_validate_json(json_str)

        assert restored.query_type == MessageMetaType.REQUEST
        assert restored.message == "What version of python?"
        assert restored.parent_task_id == "task_123"
        assert restored.choice_mode == "single"
        assert restored.choices[0].option_id == "python_313"
        assert restored.choices[0].recommended is True
        assert restored.allow_free_text is False
        rendered = render_task_query_text(restored)
        assert "Choose one option:" in rendered
        assert "1. Python 3.13 (recommended) [python_313]" in rendered
        assert "Reply with one option id or number." in rendered

    def test_control_instruction_serialization(self):
        """Test ControlInstruction round-trip serialization."""
        instruction = Instruction(instruction="pause")
        assert instruction.message_class == "Instruction"

        control = ControlInstruction(
            type=ControlType.CONTROL_MSG,
            instruction=instruction
        )
        assert control.message_class == "ControlInstruction"
        assert control.get_id() == control.instruction_id

        json_str = control.model_dump_json()
        restored = ControlInstruction.model_validate_json(json_str)

        assert restored.type == ControlType.CONTROL_MSG
        assert restored.instruction.instruction == "pause"

    def test_agent_status_serialization(self):
        """Test AgentStatus round-trip serialization."""
        status = AgentStatus(
            named_agent_type="SolverAgent",
            self_id="agent_123",
            is_paused=False,
            is_executing=True,
            has_execution_capacity=False,
            is_busy=True,
            top_level_queue_depth=2,
            waiting_top_level_task_count=1,
            deferred_message_count=3,
            name="Solver",
            agent_role="Software Engineer",
            status_type=MessageMetaType.RESPONSE,
            tools=["terminal", "browser"],
            diagnostic={"memory": "128MB"}
        )
        assert status.message_class == "AgentStatus"
        assert status.get_id() == status.agent_status_id

        json_str = status.model_dump_json()
        restored = AgentStatus.model_validate_json(json_str)

        assert restored.self_id == "agent_123"
        assert restored.tools == ["terminal", "browser"]
        assert restored.diagnostic == {"memory": "128MB"}
        assert restored.is_executing is True
        assert restored.has_execution_capacity is False

    def test_agent_creation_request_serialization(self):
        """Test AgentCreationRequest round-trip serialization."""
        req = AgentCreationRequest(
            named_agent_type="ReviewAgent",
            agent_quantity=2,
            idle_ttl_seconds=30,
        )
        assert req.message_class == "AgentCreationRequest"
        assert req.get_id() == req.request_id

        json_str = req.model_dump_json()
        restored = AgentCreationRequest.model_validate_json(json_str)

        assert restored.named_agent_type == "ReviewAgent"
        assert restored.agent_quantity == 2
        assert restored.team_id == "NO_TEAM"
        assert restored.idle_ttl_seconds == 30

    def test_scheduled_message_serialization(self):
        """Test ScheduledMessage round-trip serialization."""
        task = Task(task_type=MessageMetaType.REQUEST, problem_statement="Wake up")
        scheduled = ScheduledMessage(
            payload=task,
            interval_ms=5000,
            repeat_count=3,
            expires_at_utc="2026-06-16T12:00:00+00:00",
        )
        assert scheduled.message_class == "ScheduledMessage"
        assert scheduled.get_id() == scheduled.schedule_id

        json_str = scheduled.model_dump_json()
        restored = ScheduledMessage.model_validate_json(json_str)

        assert restored.interval_ms == 5000
        assert restored.repeat_count == 3
        assert restored.expires_at_utc == "2026-06-16T12:00:00+00:00"
        assert restored.payload.problem_statement == "Wake up"
        assert restored.enabled is True

    def test_message_envelope_union_serialization(self):
        """Test that CommsMessage union deserializes to the correct content type."""
        task = Task(task_type=MessageMetaType.REQUEST, problem_statement="Test")
        envelope = MessageEnvelope(
            msg_header=MessageHeader(to_routing_path="test_q", agent_signature=""),
            msg_meta=MessageMeta(from_agent_name="TestAgent"),
            msg_content=task
        )

        json_str = envelope.model_dump_json()
        restored = MessageEnvelope.model_validate_json(json_str)

        assert isinstance(restored.msg_content, Task)
        assert restored.msg_header.to_routing_path == "test_q"
        assert restored.msg_meta.from_agent_name == "TestAgent"


class TestEnumValidation:
    """Unit tests for Pydantic Enum validation."""

    def test_invalid_enum_raises(self):
        """Verify invalid enum values raise validation errors."""
        with pytest.raises(ValidationError):
            Task(task_type="INVALID_TYPE") # MessageMetaType

        with pytest.raises(ValidationError):
            AgentStatus(
                named_agent_type="Agent",
                self_id="123",
                is_paused=False,
                is_executing=False,
                is_busy=False,
                name="Agent",
                agent_role="Role",
                status_type="INVALID"
            )

    def test_routing_type_enum(self):
        """Test the semantic RoutingType enum."""
        header = MessageHeader(
            agent_signature="",
            to_routing_path=RoutingType.ALL_AGENTS_ROUTE
        )
        assert header.to_routing_path == "++<all>++"


class TestEdgeCases:
    """Unit tests for edge cases in message processing."""

    def test_extra_fields_allowed(self):
        """Test that MessageMeta allows unknown fields without error."""
        meta = MessageMeta(
            unknown_field="I am extra",
            from_agent_name="Bob",
            conversation_id="conversation_id_test1234",
            followup_to_task_id="task_id_prev5678",
            request_timezone="America/Chicago",
        )
        json_str = meta.model_dump_json()

        restored = MessageMeta.model_validate_json(json_str)
        # We can't access unknown_field cleanly as an attribute on restored object without dict access in Pydantic v2
        # but it shouldn't error during deserialization.
        assert restored.from_agent_name == "Bob"
        assert restored.conversation_id == "conversation_id_test1234"
        assert restored.followup_to_task_id == "task_id_prev5678"
        assert restored.request_timezone == "America/Chicago"

    def test_deep_nesting(self):
        """Test deeply nested objects."""
        answer = TaskAnswer(answer="Nested")
        task = Task(task_type=MessageMetaType.RESPONSE, answer=answer)
        scheduled = ScheduledMessage(payload=task, interval_ms=1000)
        env = MessageEnvelope(msg_content=scheduled, msg_header=MessageHeader(agent_signature=""))

        json_str = env.model_dump_json()
        restored = MessageEnvelope.model_validate_json(json_str)

        assert isinstance(restored.msg_content, ScheduledMessage)
        assert isinstance(restored.msg_content.payload, Task)
        assert isinstance(restored.msg_content.payload.answer, TaskAnswer)
        assert restored.msg_content.payload.answer.answer == "Nested"


class TestMalformedMessages:
    """Unit tests for handling invalid or malformed envelope data."""

    def test_raw_invalid_json(self):
        """Verify a completely malformed JSON message doesn't crash the decoder but fails cleanly."""
        with pytest.raises(Exception):
            MessageEnvelope.model_validate_json("{invalid_json")

    def test_valid_json_invalid_schema(self):
        """Verify a valid JSON object without the required CommsMessage structure fails validation."""
        bad_data = {"random_key": "random_value"}
        with pytest.raises(ValidationError):
            MessageEnvelope.model_validate_json(json.dumps(bad_data))
