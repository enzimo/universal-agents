"""A process that runs another message contract starts no agent (custom_agents.contract_drain).

Every start goes through ``AgentInstantiationService.launch``: the model's ``instantiate_agent``
tool, delegation's replacement for a dead agent's work, the maintenance worker, worker pools and
scheduled workers. Before, only the pool and scheduled-worker controllers checked the drain, so
the lead's takeover of dead agents' work started a replacement at every scheduler pass, and the
model could start agents, in a process that was meant to take no new work. Real SQLite launch
store with the scheduled-worker test lead, whose launcher records starts instead of running
threads.
"""
# ruff: noqa: F811 -- imported pytest fixture is injected by name

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import pytest

from custom_agents.agent_instantiation_service import LAUNCH_REFUSED_OTHER_CONTRACT, AgentInstantiationService
from custom_agents.contract_drain import ContractDrain
from custom_agents.orphaned_work_takeover import take_over_orphaned_work
from custom_messages.contracts.control_v1 import AgentCreationRequest
from custom_messages.contracts.schedule_worker_v1 import AgentLaunchState
from storage.agent_launch_repository import AgentLaunchRepository
from tests.test_orphaned_work_takeover import held_work, launch_record
from tests.test_schedule_worker_service import lead  # noqa: F401

REASON = "Its process runs another message contract than its installation."


def _drain(agent) -> None:
    agent._contract_drain = ContractDrain(reason=REASON, started_at=datetime.now(timezone.utc))


def _refusals(caplog) -> list[str]:
    return [record.getMessage() for record in caplog.records
            if "event=agent_launch_refused_other_contract" in record.getMessage()]


@pytest.mark.parametrize("source", ["manual", "delegation", "pool", "schedule"])
def test_a_draining_process_starts_no_agent_from_any_source(lead, caplog, source) -> None:
    _drain(lead)
    with caplog.at_level(logging.WARNING):
        result = AgentInstantiationService(lead).launch(agent_type="Worker", owner_id="owner",
                                                        request_id="start-1", source=source)
    assert (result.status, result.reason) == ("denied", LAUNCH_REFUSED_OTHER_CONTRACT)
    assert lead.starts == []
    assert AgentLaunchRepository(lead.task_manager.storage_settings).list("team") == ()
    assert len(_refusals(caplog)) == 1 and f"source={source}" in _refusals(caplog)[0]


def test_the_model_is_told_plainly_that_the_process_takes_no_new_work(lead, caplog, monkeypatch) -> None:
    from custom_tools.providers import agent_management

    _drain(lead)
    lead.handle_agent_creation_request = (
        lambda request, header, meta: AgentInstantiationService(lead).create(request))
    monkeypatch.setattr(agent_management, "get_agent_ref", lambda: lead)
    with caplog.at_level(logging.WARNING):
        answer = json.loads(agent_management.instantiate_agent(agent_type="Worker", team_id="team",
                                                               idle_ttl_seconds=60.0))
        three = AgentInstantiationService(lead).create(AgentCreationRequest(
            named_agent_type="Worker", agent_quantity=3, team_id="team"))
    assert answer["reason_code"] == LAUNCH_REFUSED_OTHER_CONTRACT
    assert "takes no new work and starts no agents" in answer["error"]
    assert [item.reason for item in three.results] == [LAUNCH_REFUSED_OTHER_CONTRACT] * 3
    assert len(_refusals(caplog)) == 2  # one line per request, not per agent
    assert lead.starts == []


@pytest.mark.asyncio
async def test_a_draining_lead_starts_no_replacement_for_a_dead_workers_work(lead, caplog) -> None:
    launch_record(lead, AgentLaunchState.FAILED)
    held_work(lead)
    lead.is_team_lead = True
    lead.same_role_runtime_takeover_enabled = True
    lead.schedule_maintenance = {}
    _drain(lead)
    with caplog.at_level(logging.INFO):
        assert await take_over_orphaned_work(lead, clock=1.0) == 0
        assert await take_over_orphaned_work(lead, clock=100.0) == 0
    assert not [start for start in lead.starts if start.named_agent_type == "Worker"]
    # Nothing is asked or logged at each pass while it drains.
    assert "event=orphaned_work_takeover" not in caplog.text and not _refusals(caplog)
    # Once a lead on the installation's contract runs, it takes the work over.
    del lead._contract_drain
    assert await take_over_orphaned_work(lead, clock=200.0) == 1


def test_a_draining_lead_cannot_create_a_delegation_that_its_pool_will_never_start(lead):
    from custom_agents.turn_action_outbox_runtime import _validate_manager_mediated_worker_delegation
    from tests.test_turn_action_outbox import _delegation

    lead.named_agent_type = "TeamArchitect"
    _validate_manager_mediated_worker_delegation(lead, _delegation("Do some work"))
    _drain(lead)
    with pytest.raises(RuntimeError, match="cannot delegate new tasks"):
        _validate_manager_mediated_worker_delegation(lead, _delegation("Do some work"))
