"""A real NATS heartbeat clears old presence after a simulated host suspension.

Use the reported Oct 9 timestamps with real SQLite custody and Inbox stores.
Only the monitor's clock is simulated; the status crosses the production
NATS/JetStream adapter and is acknowledged after it is persisted.
"""
# ruff: noqa: F811 -- imported pytest fixture is injected by name

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from custom_agents.liveness_observation import observe_liveness
from custom_agents.stuck_tasks import report_stuck_tasks
from custom_messages.contracts.control_v1 import AgentStatus
from custom_messages.enums import RoutingType
from tests.test_live_nats_replacement_semantics import (
    _adapter,
    _publish,
    _status,
    _subscribe_queue,
    _team,
)
from tests.test_stuck_tasks import (
    admin_notices,
    heartbeat_at,
    lead,  # noqa: F401
    maintenance_record,
    notices,
    open_incidents,
    save,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration, pytest.mark.live, pytest.mark.nats]


async def test_live_heartbeat_after_host_resume_keeps_maintenance_custody(lead, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(
        "custom_agents.liveness_observation.time",
        SimpleNamespace(monotonic=lambda: clock[0]),
    )
    before = datetime(2026, 10, 9, 19, 36, 49, tzinfo=timezone.utc)
    woke = datetime(2026, 10, 9, 19, 54, 15, tzinfo=timezone.utc)
    save(lead, "maintenance-worker", "InternalImprovementAgent", [maintenance_record(monkeypatch, before)])
    snapshot = lead.task_manager.runtime_state_store.peek_task_runtime_by_team("team")[0]
    heartbeat_at(lead, before)
    observe_liveness(lead, now=before)

    adapter = _adapter(_team("resume_presence"))
    try:
        await adapter.connect(reconcile_topology=True)
        queue = await _subscribe_queue(
            adapter,
            name="resume-presence",
            subject=f"{adapter.mapper.prefix}.presence.*",
            durable_name=adapter.mapper.durable_name("resume_presence"),
            deliver_last_per_subject=True,
        )
        clock[0] += 1
        observe_liveness(lead, now=woke)
        await report_stuck_tasks(lead, now=woke)
        assert not admin_notices(lead) and not open_incidents(lead)

        envelope = _status("maintenance-worker", "team")
        envelope.msg_content.named_agent_type = "InternalImprovementAgent"
        envelope.msg_content.agent_role = "worker"
        envelope.msg_content.last_seen_at = (woke + timedelta(seconds=5)).isoformat()
        await _publish(adapter, RoutingType.ALL_AGENTS_ROUTE.value, envelope, presence=True)
        delivery = await asyncio.wait_for(queue.get(), timeout=5)
        status = delivery.envelope.msg_content
        assert isinstance(status, AgentStatus)
        lead._agent_state_store.upsert(status)
        await delivery.ack()

        clock[0] += 60
        observe_liveness(lead, now=woke + timedelta(seconds=60))
        await report_stuck_tasks(lead, now=woke + timedelta(seconds=60))
        [held] = lead.task_manager.runtime_state_store.peek_task_runtime_by_team("team")
        assert held.owner_agent_id == "maintenance-worker"
        assert held.runtime_state_json == snapshot.runtime_state_json
        assert not admin_notices(lead) and not notices(lead) and not open_incidents(lead)
    finally:
        await adapter.close()
