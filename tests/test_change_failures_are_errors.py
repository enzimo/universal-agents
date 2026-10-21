"""A call that changes a record and fails returns a failed result, not a success.

Strands reads a call's outcome only from its result status, and the tool
executor stops a model step at the first record change that does not succeed
(``custom_tools.tool_results``). Every such tool and action is called here
without an agent, which each refuses before changing anything; each must
return status ``error`` with its JSON reason. Other paths (refusals, missing
records, staged changes) are checked in each tool's own tests.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from custom_tools.providers.heartbeat import (
    add_heartbeat_item,
    disable_heartbeat_item,
    enable_heartbeat_item,
    remove_heartbeat_item,
    update_heartbeat_item,
)
from custom_tools.providers.knowledge import knowledge_add_note, knowledge_archive, knowledge_write
from custom_tools.providers.knowledge_curation import (
    freeze_knowledge_page,
    merge_knowledge_pages,
    publish_contribution,
    reject_contribution,
    resolve_knowledge_note,
    revert_knowledge_page,
    unfreeze_knowledge_page,
)
from custom_tools.providers.memory import memory
from custom_tools.providers.objective_work import objective_work_profiles, objective_work_sessions
from custom_tools.providers.objectives import (
    activate_plan_execution,
    create_objective,
    objectives,
    record_objective_work_checkpoint,
    start_objective_task,
)
from custom_tools.providers.schedule_workers import configure_scheduled_worker
from custom_tools.providers.schedules import (
    create_scheduled_job,
    disable_scheduled_job,
    pause_scheduled_job,
    remove_scheduled_job,
    resume_scheduled_job,
    update_scheduled_job,
)
from custom_tools.runtime_context import set_agent_ref

# A schedule version as list_scheduled_jobs returns it (record_digest).
SCHEDULE_VERSION = "0" * 64

CHANGES: list[tuple[Any, dict[str, Any]]] = [
    (memory, {"action": "create", "bank": "coremem", "fact_text": "Ships on Fridays."}),
    (memory, {"action": "replace", "bank": "coremem", "memory_id": 1, "fact_text": "Ships on Mondays."}),
    (memory, {"action": "delete", "bank": "coremem", "memory_id": 1}),
    (memory, {"action": "undo", "bank": "coremem", "event_id": "event_1"}),
    (knowledge_write, {"page": "personal/notes", "title": "Notes", "body": "Mine."}),
    (knowledge_add_note, {"page": "personal/notes", "note": "Later."}),
    (knowledge_archive, {"page": "personal/notes", "base_revision": 1, "reason": "Done."}),
    (publish_contribution, {"contribution_id": "contribution_1"}),
    (reject_contribution, {"contribution_id": "contribution_1", "reason": "Outdated."}),
    (resolve_knowledge_note, {"note_id": "note_1", "merged": False}),
    (freeze_knowledge_page, {"page": "team/notes", "minutes": 60}),
    (unfreeze_knowledge_page, {"page": "team/notes"}),
    (merge_knowledge_pages, {"source": "team/a", "target": "team/b", "body": "Merged.", "base_revision": 1, "source_revision": 1}),
    (revert_knowledge_page, {"page": "team/notes", "to_revision": 1, "base_revision": 2, "reason": "Restore."}),
    (add_heartbeat_item, {"instruction": "Check the build."}),
    (update_heartbeat_item, {"heartbeat_item_id": "hb_1", "expected_version": "v1", "title": "Build"}),
    (remove_heartbeat_item, {"heartbeat_item_id": "hb_1", "expected_version": "v1"}),
    (enable_heartbeat_item, {"heartbeat_item_id": "hb_1", "expected_version": "v1"}),
    (disable_heartbeat_item, {"heartbeat_item_id": "hb_1", "expected_version": "v1"}),
    (objectives, {"action": "update_progress", "objective_id": "obj_1", "progress_summary": "Half done."}),
    (objectives, {"action": "update_status", "objective_id": "obj_1", "status": "paused", "summary": "Waiting."}),
    (record_objective_work_checkpoint, {"observe": "o", "orient": "o", "decide": "d", "act": "a", "health": "ok"}),
    (create_objective, {"title": "Ship", "desired_outcome": "Shipped.", "why": "Customers wait."}),
    (start_objective_task, {"work_brief": "Continue."}),
    (activate_plan_execution, {"steps_json": "[]"}),
    (objective_work_sessions, {"action": "enable", "objective_id": "obj_1", "confirmation_summary": "Yes."}),
    (objective_work_sessions, {"action": "disable", "objective_id": "obj_1", "confirmation_summary": "Yes."}),
    (objective_work_sessions, {"action": "start", "objective_id": "obj_1"}),
    (objective_work_profiles, {"action": "apply", "proposal_id": "p_1", "proposal_digest": "d", "confirmation_summary": "Yes."}),
    (create_scheduled_job, {"instruction": "Check the build.", "interval_seconds": 3600}),
    (update_scheduled_job, {"schedule_id": "sched_1", "expected_record_digest": SCHEDULE_VERSION, "interval_seconds": 60}),
    (remove_scheduled_job, {"schedule_id": "sched_1", "expected_record_digest": SCHEDULE_VERSION}),
    (disable_scheduled_job, {"schedule_id": "sched_1", "expected_record_digest": SCHEDULE_VERSION}),
    (resume_scheduled_job, {"schedule_id": "sched_1", "expected_record_digest": SCHEDULE_VERSION}),
    (pause_scheduled_job, {"schedule_id": "sched_1", "expected_record_digest": SCHEDULE_VERSION, "duration_minutes": 30}),
    (configure_scheduled_worker, {"schedule_id": "sched_1", "target_agent_type": "Worker", "expected_revision": 0}),
]


@pytest.mark.parametrize(
    ("tool", "inputs"),
    CHANGES,
    ids=[f"{tool.tool_name}-{inputs.get('action', '')}" for tool, inputs in CHANGES],
)
def test_a_refused_change_is_a_failed_result(tool: Any, inputs: dict[str, Any]) -> None:
    set_agent_ref(None)

    result = tool(**inputs)

    assert isinstance(result, dict) and result["status"] == "error", result
    assert "error" in json.loads(result["content"][0]["text"])
