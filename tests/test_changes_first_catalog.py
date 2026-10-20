"""The shipped catalog orders curator writes and real approved schedule changes.

These regressions exercise the catalog together with Strands rather than a
test-only list of mutations. The schedule test also uses real Cedar, signed
task identity, the change Inbox and SQLite, with only the model scripted.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from strands import Agent
from strands.interrupt import _InterruptState

from custom_agents import access_runtime
from custom_agents.default_tools import builtin_tool_catalog
from custom_agents.sdk.strands.changes_first import ChangesFirstToolExecutor
from custom_messages.contracts.access_v2 import AccessLevel, AccessReviewChoice
from custom_messages.enums import ChangeRecordKind
from custom_tools.providers.changes import apply_change
from custom_tools.providers.knowledge_curation import publish_contribution
from custom_tools.providers.schedules import disable_scheduled_job, pause_scheduled_job
from custom_tools.runtime_context import set_agent_ref
from tests.change_approval_support import needs_cedar, owner, use_signed_contexts
from tests.test_changes_first_executor import CALLS, DenyCall, ScriptedModel, not_run, read_note, step_results
from tests.test_system_schedule_changes import _lead, _schedule, _stored


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        (publish_contribution, {"contribution_id": "kc_pending"}),
        (apply_change, {"change_id": "sys_sched_chg_pending", "reason": "Pause for maintenance."}),
    ],
    ids=["curator-publication", "staged-plan-application"],
)
def test_a_denied_catalog_mutation_skips_the_steps_other_calls(tool, arguments) -> None:
    CALLS.clear()
    agent = Agent(
        model=ScriptedModel([("read_note", {}), (tool.tool_name, arguments)]),
        tools=[read_note, tool],
        callback_handler=None,
        hooks=[DenyCall(tool.tool_name)],
        tool_executor=ChangesFirstToolExecutor(builtin_tool_catalog().record_kind),
    )

    asyncio.run(agent.invoke_async("go"))

    assert CALLS == []
    results = step_results(agent)
    assert not_run(results[0])
    assert results[1]["status"] == "error" and not not_run(results[1])


@pytest.mark.parametrize(
    ("tool_id", "kind"),
    [
        ("fn:knowledge", ChangeRecordKind.KNOWLEDGE_PAGE),
        ("fn:knowledge_curation", ChangeRecordKind.KNOWLEDGE_PAGE),
        ("fn:changes", ChangeRecordKind.STAGED_CHANGE),
    ],
)
def test_every_mutation_in_the_knowledge_and_change_tools_is_ordered(tool_id, kind) -> None:
    """New writes in these record-owning tools must not silently run among reads."""

    catalog = builtin_tool_catalog()
    bindings = [binding for binding in catalog.functions.values() if binding.tool_id == tool_id]
    assert bindings
    for binding in bindings:
        assert binding.argument is None
        expected = None if binding.level is AccessLevel.READ else kind
        assert catalog.record_kind(binding.function) is expected, binding.function


@needs_cedar
def test_a_real_system_schedule_approval_skips_calls_that_stay_skipped_on_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_signed_contexts(monkeypatch)
    CALLS.clear()
    lead = _lead(tmp_path, authorization_enabled=True)
    _schedule(lead, "hb_sched_team", system=True)
    _schedule(lead, "sched_personal")
    set_agent_ref(lead)
    staged = json.loads(pause_scheduled_job("hb_sched_team", duration_minutes=30))
    assert staged["status"] == "approval_required"
    agent = Agent(
        model=ScriptedModel(
            [
                ("read_note", {}),
                ("apply_change", {"change_id": staged["change_id"], "reason": "Pause for maintenance."}),
                ("disable_scheduled_job", {"schedule_id": "sched_personal"}),
            ]
        ),
        tools=[read_note, apply_change, disable_scheduled_job],
        callback_handler=None,
        tool_executor=ChangesFirstToolExecutor(builtin_tool_catalog().record_kind),
    )
    try:
        paused = asyncio.run(agent.invoke_async("go"))

        assert paused.stop_reason == "interrupt"
        assert CALLS == []
        assert _stored(lead, "hb_sched_team").paused_until is None
        pending = agent._interrupt_state.pending_tool_execution
        assert pending is not None
        assert {result["toolUseId"] for result in pending.completed_tool_results} == {"call-0", "call-2"}
        assert all(not_run(result) for result in pending.completed_tool_results)
        # Exercise the persisted pause used by a session restore, rather than
        # keeping only the live SDK objects from the first invocation.
        agent._interrupt_state = _InterruptState.from_dict(json.loads(json.dumps(agent._interrupt_state.to_dict())))
        interrupt = paused.interrupts[0]
        requests = access_runtime.runtime_requests(lead)
        decision = requests.decide(interrupt.reason["access_request_id"], owner(lead), AccessReviewChoice.APPROVE)

        asyncio.run(
            agent.invoke_async(
                [{"interruptResponse": {"interruptId": interrupt.id, "response": {"status": decision.status.value}}}]
            )
        )

        results = {result["toolUseId"]: result for result in step_results(agent)}
        assert results["call-1"]["status"] == "success"
        assert json.loads(results["call-1"]["content"][0]["text"])["status"] == "applied"
        assert _stored(lead, "hb_sched_team").paused_until is not None
        assert _stored(lead, "sched_personal").enabled is True
        assert not_run(results["call-0"]) and not_run(results["call-2"])
        assert CALLS == []
    finally:
        set_agent_ref(None)
        access_runtime._runtime_access.cache_clear()
