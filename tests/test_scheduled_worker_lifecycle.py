"""Actual local worker threads, Strands turns and JetStream with isolated storage."""

import asyncio
import json
import os
from datetime import timedelta
from uuid import uuid4

import pytest
from strands.models import Model

from config_loader import Config
from custom_agents.schedule_worker_controller import local_schedule_runtime_loop
from custom_messages.contracts.routing_v1 import (
    GatewayContext,
    MessageHeader,
    MessageMeta,
)
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.contracts.schedule_worker_v1 import (
    AgentLaunchState,
    utc_now,
)
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import (
    MessageMetaType,
    ProgressState,
    ScheduleManagementClass,
)


class ScheduledResultModel(Model):
    """Use the SDK's real structured-output tool protocol with deterministic content."""

    calls = 0

    def update_config(self, **model_config):
        pass

    def get_config(self):
        return {"model_id": "scheduled-result-test"}

    async def structured_output(self, *args, **kwargs):
        raise AssertionError("The runtime uses the structured-output tool protocol")
        yield

    async def stream(self, messages, tool_specs=None, *args, **kwargs):
        type(self).calls += 1
        # Answer with the turn-answer tool the runtime offers: specialists get
        # ModelWorkerTurnDecision, the team lead ModelAgentTurnDecision.
        [decision_tool] = [spec["name"] for spec in tool_specs or () if spec["name"].endswith("TurnDecision")]
        payload = {
            "actions": [
                {
                    "action_type": "task_response",
                    "task": {"answer": "Scheduled report complete"},
                    "suppress_outbound_dispatch": True,
                }
            ]
        }
        yield {"messageStart": {"role": "assistant"}}
        yield {
            "contentBlockStart": {
                "start": {
                    "toolUse": {
                        "toolUseId": uuid4().hex,
                        "name": decision_tool,
                    }
                }
            }
        }
        yield {
            "contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(payload)}}}
        }
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "tool_use"}}


@pytest.fixture
def local_broker():
    # The live profile supplies a broker; unique team IDs isolate test subjects.
    return os.environ["NATS_URL"]


@pytest.mark.parametrize("memory_timer", [False, True])
@pytest.mark.live
@pytest.mark.integration
@pytest.mark.nats
@pytest.mark.asyncio
async def test_three_occurrences_spawn_retire_and_complete_without_lead_turn(
    tmp_path, monkeypatch, local_broker, memory_timer, caplog
):
    from custom_agents import strands_team
    from custom_agents.agent_instantiation_service import AgentInstantiationService
    from custom_agents.schedule_store import ScheduleStore

    team = "team_" + uuid4().hex[:12]
    from custom_transport.nats_adapter import NatsTransportAdapter, NatsTransportConfig
    from custom_transport.subjects import NatsSubjectMapper

    topology = NatsTransportAdapter(
        config=NatsTransportConfig(servers=[local_broker]),
        mapper=NatsSubjectMapper(team_id=team),
    )
    await topology.connect(reconcile_topology=True)
    await topology.close()
    database = str(tmp_path / "runtime.db")
    config = Config(
        {
            "transport": {"nats_servers": [local_broker]},
            "storage": {"backend": "sqlite", "sqlite": {"path": database}},
            "agent": {
                "workspace_dir": str(tmp_path),
                "skills_enabled": False,
                "memory_graph_enabled": False,
                "schedule_workers": {
                    "eligible_agent_types": [],
                    "reconcile_interval_seconds": 1,
                    "defaults": {
                        "claim_wait_seconds": 0,
                        "startup_timeout_seconds": 15,
                        "admission_timeout_seconds": 1,
                        "idle_ttl_seconds": 1,
                    },
                },
            },
            "authorization": {"enabled": False},
            "llm": {"provider": "openai", "model_id": "scheduled-result-test"},
        }
    )
    monkeypatch.setattr("config_loader._config", config)
    worker_type = "MemReviewAgent" if memory_timer else "Worker"
    # A shipped type, so the default startup policy covers it without review.
    definition = {
        "named_agent_type": worker_type,
        "name": "Worker",
        "role": "Report worker",
        "goal": "Return the scheduled report",
        "tools": [],
        "skills_enabled": False,
        "agent_definition_source": "release",
        "declared_definition": {
            "role": "Report worker",
            "own_prompt": "Return the scheduled report",
            "include_shared_prompt": False,
            "backstory": "hub_and_spoke",
            "default_tools": [],
            "skills_enabled": False,
        },
    }
    monkeypatch.setattr(
        strands_team, "load_agent_type_map", lambda: {worker_type: definition}
    )
    monkeypatch.setattr(
        "custom_agents.strands_agent.build_strands_model",
        lambda *args, **kwargs: ScheduledResultModel(),
    )
    lead = strands_team.create_strands_agent(
        {
            **definition,
            "named_agent_type": "TeamArchitect",
            "team_id": team,
            "is_team_lead": True,
            "task_history_db_path": database,
            "workspace_dir": str(tmp_path),
            "authorization_config": {"enabled": False},
            "memory_graph_enabled": False,
            "skills_enabled": False,
            "model_provider": "openai",
            "model_id": "scheduled-result-test",
            "nats_servers": [local_broker],
        }
    )
    lead.task_manager.add_task(
        Task(
            task_id="setup",
            task_type=MessageMetaType.REQUEST,
            problem_statement="Schedule a report",
        ),
        MessageHeader(),
        MessageMeta(gateway_context=GatewayContext(canonical_user_id="owner")),
        owner_person_id="owner",
    )
    lead.task_manager.update_main_task_state(
        "setup", ProgressState.IN_PROGRESS, utc_now().isoformat()
    )
    lead._schedule_store = ScheduleStore(
        storage_settings=lead.task_manager.storage_settings
    )
    schedule = ScheduledMessage(
        schedule_id="recurring",
        management_class=ScheduleManagementClass.SYSTEM if memory_timer else ScheduleManagementClass.USER,
        schedule_name="memory_review:coremem:live" if memory_timer else None,
        payload=Task(
            task_type=MessageMetaType.REQUEST,
            problem_statement="Return the scheduled report",
        ),
        payload_meta=MessageMeta(
            gateway_context=GatewayContext(
                canonical_user_id="owner", scheduled_job_id="recurring"
            )
        ),
        claim_team_id=team,
        claim_named_agent_type="TeamArchitect" if memory_timer else worker_type,
        interval_ms=5000,
        repeat_count=3,
        enabled=True,
        next_fire_at=(utc_now() - timedelta(seconds=1)).isoformat(),
    )
    from custom_agents.schedule_conversation_context import ensure_dedicated_schedule_conversation

    schedule, conversation = ensure_dedicated_schedule_conversation(lead, schedule)
    assert (conversation is None) is memory_timer
    lead._schedule_store.add(schedule)
    if memory_timer:
        from custom_agents.local_schedule_upgrade import apply_local_schedule_upgrade

        assert apply_local_schedule_upgrade(lead)[0]["action"] == "upgraded"
    lead.paused = True
    supervisor = asyncio.create_task(local_schedule_runtime_loop(lead))
    try:
        async with asyncio.timeout(75):
            while True:
                launches = AgentInstantiationService(lead).repository.list(team)
                assert all(
                    item.state != AgentLaunchState.FAILED for item in launches
                ), "A real worker failed before readiness"
                if len(launches) == 3 and all(
                    item.state == AgentLaunchState.RETIRED for item in launches
                ):
                    break
                await asyncio.sleep(0.15)
        assert len({item.agent_id for item in launches}) == 3
        assert all(item.ready_at_utc for item in launches)
        assert all(
            lead._schedule_store.worker_records.get_occurrence(item.occurrence_id, team)
            for item in launches
        )
        for item in launches:
            occurrence = lead._schedule_store.worker_records.get_occurrence(
                item.occurrence_id, team
            )
            completed = lead.task_manager.get_task_from_db(
                occurrence.task_id, include_all_agents=True
            )
            assert (
                completed is not None
                and completed["status"] == ProgressState.COMPLETED.value
            )
            assert occurrence.outcome == ProgressState.COMPLETED.value
            turn = lead._conversation_store.get_turn_by_task_id(occurrence.task_id)
            assert turn is not None
            if conversation is not None:
                assert turn["conversation_id"] == conversation["conversation_id"]
            assert turn["followup_to_task_id"] is None
            assert turn["delivery_state"] == "suppressed"
            assert "task_response_suppression_cleared" not in str(completed)
        assert not lead.is_executing
        assert lead.task_manager.current_task_id == "setup"
        # The lead's start ran the one-time fingerprint upgrade on its task
        # lane; it failed at every start through 0.9.1.
        assert "event=schedule_execution_digest_upgrade_failed" not in caplog.text
    finally:
        supervisor.cancel()
        await asyncio.gather(supervisor, return_exceptions=True)
        strands_team.shutdown_spawned_agents(team, lead.id, 10)
