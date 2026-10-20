"""Exercise agent queue tools against the real persisted task manager.

A turn works on one request (its task's signed context). It may pause, resume
or stop only tasks of that request, such as another delegated task of it in a
worker's queue; the person's other requests are theirs to control.
"""

import json

import pytest

from custom_messages.enums import ProgressState
from custom_tools.providers.task_queue import control_task_queue, list_task_queue
from custom_tools.runtime_context import set_agent_ref
from test_runtime_task_controls import agent as runtime_agent_fixture, add, command
from tests.broker_access_support import context_signer
from tests.change_approval_support import use_signed_contexts


@pytest.fixture(params=[True, False], ids=["lead", "worker"])
def agent(request, tmp_path):
    # Reuse the production-shaped fixture for both lead and specialist cases.
    return runtime_agent_fixture.__wrapped__(tmp_path, request)


def sign(agent, task_id: str, request=None):
    """Give a task the gateway-signed context of its owner's request (a new request by default)."""

    signed = request or context_signer().issue(team_id=agent.team_id, person_id="owner", person_roles=("owner",))
    agent.task_manager.get_task_info(task_id).meta.gateway_context.task_context = signed
    return signed


@pytest.fixture(autouse=True)
def current_request(agent, monkeypatch):
    """The request the running task ("inspection") works on."""

    # The queue tools act for the person in the running task's signed context.
    use_signed_contexts(monkeypatch)
    add(agent, "inspection", state=ProgressState.IN_PROGRESS)
    request = sign(agent, "inspection")
    set_agent_ref(agent)
    yield request
    set_agent_ref(None)


@pytest.mark.asyncio
async def test_inspect_pause_resume_stop_own_queue(agent, current_request):
    # Another task of the request this turn works on.
    add(agent, "queued")
    sign(agent, "queued", current_request)
    add(agent, "private", owner="someone_else")
    listing = json.loads(await list_task_queue())
    assert {row["root_task_id"] for row in listing["tasks"]} == {"inspection", "queued"}
    paused = json.loads(await control_task_queue("queued", "pause", "Review duplicate"))
    assert paused["state"] == "paused"
    assert "queued" not in agent.task_manager.task_state.task_queue
    resumed = json.loads(
        await control_task_queue("queued", "resume", "Retain this task")
    )
    assert resumed["state"] == "accepted"
    assert "queued" in agent.task_manager.task_state.task_queue
    stopped = json.loads(
        await control_task_queue("queued", "stop", "Confirmed duplicate")
    )
    assert stopped["durable_cancellation_recorded"]
    assert agent.task_manager.get_task_info("queued") is None
    assert agent.task_manager.get_task_from_db("queued")["status"] == "cancelled"


@pytest.mark.asyncio
async def test_queue_rejects_other_owner_self_and_pending_approval(agent, current_request):
    add(agent, "private", owner="someone_else")
    add(agent, "approval", state=ProgressState.WAITING)
    sign(agent, "approval", current_request)
    agent.task_manager.task_state.human_interaction_blockers["approval"] = {
        "permission_request_id": "permission-1"
    }
    for task_id, action in [
        ("private", "stop"),
        ("inspection", "stop"),
        ("approval", "resume"),
    ]:
        result = json.loads(await control_task_queue(task_id, action, "Review"))
        assert "error" in result
    assert agent.task_manager.get_task_info("approval").state == ProgressState.WAITING
    assert "approval" not in agent.task_manager.task_state.task_queue


@pytest.mark.asyncio
async def test_explicit_queue_pause_survives_recovery_and_does_not_mask_a_later_orphan(
    agent, current_request,
):
    from custom_agents.runtime_recovery import _preserve_or_requeue_recovered_pause
    from custom_agents.runtime_task_controls import has_explicit_queue_pause

    add(agent, "queued")
    sign(agent, "queued", current_request)
    paused = json.loads(await control_task_queue("queued", "pause", "Review tomorrow"))
    assert paused["state"] == "paused", paused
    from custom_agents.task_manager import TaskManager

    restored = TaskManager(db_path=agent.task_manager._db_path)
    restored.set_owner_identity("lead", "Lead", "team", "TeamArchitect")
    assert restored.restore_runtime_state()
    agent.task_manager = restored
    assert has_explicit_queue_pause(agent.task_manager, "queued")
    await _preserve_or_requeue_recovered_pause(
        agent, task_id="queued", now_ts="2026-09-22T00:00:00Z"
    )
    assert agent.task_manager.get_task_info("queued").state == ProgressState.PAUSED
    assert "queued" not in agent.task_manager.task_state.task_queue
    # Restart restores records but releases execution custody. A new user turn
    # must be active before an agent tool has a canonical user context again.
    add(agent, "after-restart", state=ProgressState.IN_PROGRESS)
    sign(agent, "after-restart", current_request)
    resumed = json.loads(await control_task_queue("queued", "resume", "Continue"))
    assert resumed["state"] == "accepted"
    agent.task_manager.update_main_task_state(
        "queued", ProgressState.PAUSED, "2026-09-23T00:00:00Z"
    )
    assert not has_explicit_queue_pause(agent.task_manager, "queued")
    await _preserve_or_requeue_recovered_pause(
        agent, task_id="queued", now_ts="2026-09-24T00:00:00Z"
    )
    assert agent.task_manager.get_task_info("queued").state == ProgressState.ACCEPTED


def queue_state(agent, task_id: str):
    manager = agent.task_manager
    info = manager.get_task_info(task_id)
    return info.state, task_id in manager.task_state.task_queue, manager.is_execution_cancelled(task_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["stop", "pause", "resume"])
async def test_a_turn_cannot_stop_pause_or_resume_the_persons_other_request(agent, action):
    # The person's other request, queued (or paused by the person, for resume).
    add(agent, "other", state=ProgressState.PAUSED if action == "resume" else None)
    sign(agent, "other")
    before = queue_state(agent, "other")

    result = json.loads(await control_task_queue("other", action, "Looks like a duplicate"))

    assert queue_state(agent, "other") == before, "the other request is unchanged"
    from custom_tools.providers.task_queue import OTHER_REQUEST_REFUSAL

    assert result == {"error": OTHER_REQUEST_REFUSAL.format(task_id="other"), "task_id": "other"}
    assert "that request's own thread" in result["error"] and "/stop task other" in result["error"]


@pytest.mark.asyncio
async def test_a_task_without_a_request_is_refused_too(agent):
    add(agent, "unsigned")  # no signed context: nothing ties it to this request

    result = json.loads(await control_task_queue("unsigned", "stop", "Not needed"))

    assert "error" in result and agent.task_manager.get_task_info("unsigned") is not None


@pytest.mark.asyncio
async def test_the_persons_own_stop_still_reaches_their_other_request(agent):
    add(agent, "other")
    sign(agent, "other")
    refused = json.loads(await control_task_queue("other", "stop", "Looks like a duplicate"))
    assert "error" in refused and agent.task_manager.get_task_info("other") is not None

    # The person removes it in Settings -> Tasks; the manager decides at once, without a model turn.
    view = await command(agent)
    removed = await command(agent, "remove_task", target_task_id="other", inventory_digest=view.inventory_digest)

    assert removed.success and removed.cancellation_receipts[0].confirmed
    assert agent.task_manager.get_task_info("other") is None
    assert agent.task_manager.get_task_from_db("other")["status"] == "cancelled"
    assert agent.task_manager.get_task_info("inspection").state == ProgressState.IN_PROGRESS
