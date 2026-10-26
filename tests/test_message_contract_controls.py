"""Protocol control bodies retain values the existing runtime can produce."""

from __future__ import annotations

import copy
from datetime import datetime

import pytest
from jsonschema import ValidationError

from custom_agents.conversation_store import ConversationStore
from custom_messages.contracts.control_v1 import PersonWorkOutcomeV1
from custom_messages.contracts.status_v1 import StatusSeverity
from custom_messages.wire.frame import format_timestamp
from tests.message_contract_support import example_frame
from tests.test_message_contract import body_schema_file, checked, validator


def test_person_standing_reports_the_actual_manager_counts():
    outcome = PersonWorkOutcomeV1(
        tasks_stopped=2, task_records_deleted=3, objectives_stopped=1,
        objectives_deleted=1, schedules_deleted=4, memory_review_jobs_deleted=1,
        held_record_count=8, restored_record_count=5, sandbox_deleted=True,
    )
    body = {"outcome": "ok", "state": "settled", **outcome.model_dump(mode="json", exclude_none=True)}
    reply = checked("people.set_standing.reply", body)
    for name, value in outcome.model_dump(exclude_none=True).items():
        assert getattr(reply, name) == value


def test_an_idle_agent_needs_no_invented_context_measurement():
    body = copy.deepcopy(example_frame("agent.status.reply", "idle")["body"])
    assert not body["executing"] and "context" not in body
    checked("agent.status.reply", body)


@pytest.mark.parametrize("severity", list(StatusSeverity))
def test_agent_status_preserves_runtime_severities_and_exceeded_budgets(severity):
    body = copy.deepcopy(example_frame("agent.status.reply", "ok")["body"])
    body["context"]["fill_per_mille"] = 1200
    body["tool_call_budget_per_mille"] = 1500
    body["findings"] = [{"finding_id": "context_pressure", "severity": severity.value, "title": "Context is over its limit"}]
    checked("agent.status.reply", body)


@pytest.mark.parametrize("replacement", [{"text": "Changed words"}, {"attachments": []}])
def test_forwarding_person_steering_cannot_substitute_model_written_content(replacement):
    body = copy.deepcopy(example_frame("steering.decide", "person-as-is")["body"])
    checked("steering.decide", body)
    body["decision"]["actions"][0]["action"].update(replacement)
    with pytest.raises(ValidationError):
        validator(body_schema_file("steering.decide")).validate(body)


def test_stale_intent_reports_the_same_integer_version_type_as_the_request():
    body = {"outcome": "changed_since_read", "current_version": 3}
    checked("intent.set.reply", body)
    body["current_version"] = "3"
    with pytest.raises(ValidationError):
        validator(body_schema_file("intent.set.reply")).validate(body)


def test_topic_lookup_preserves_a_new_threads_stored_active_status(tmp_path):
    store = ConversationStore(str(tmp_path / "conversation.db"), artifacts_dir=tmp_path / "artifacts")
    store.ensure_thread(
        conversation_id="conversation_research", team_id="team_example",
        owner_agent_id="lead", owner_agent_name="TeamLead", requester_name="person_7c1",
        requester_id="person_7c1", root_task_id="task_research", owner_canonical_user_id="person_7c1",
    )
    store.tag_thread(conversation_id="conversation_research", tag="research", team_id="team_example", canonical_user_id="person_7c1")
    [thread] = store.find_threads_by_tags(tags=["research"], team_id="team_example", canonical_user_id="person_7c1")
    assert thread["status"] == "active"
    checked("topics.find_threads.reply", {
        "outcome": "ok", "matches": [{
            "conversation_id": thread["conversation_id"], "status": thread["status"],
            "updated_at": format_timestamp(datetime.fromisoformat(thread["updated_at"])),
            "matched_tags": thread["matched_tags"],
        }],
    })
