"""Request ids on AgentStatus never leave the gateway (Web Chat agent trail, plan review C08).

The gateway's team view fills ``AgentStatus.request_task_ids`` from each agent's
signed record so the thread list can say who works on a person's request. The
ids name one person's requests, so no dump may carry them: not a status reply,
not a model's view, not the Team page or the activity mesh.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from assistant_gateway.team_presence import GatewayTeamPresence
from assistant_gateway.team_view import record_status
from custom_messages.contracts.control_v1 import AGENT_STATUS_RUNTIME_ONLY_FIELDS, AgentStatus
from custom_messages.enums import MessageMetaType
from custom_transport.protocol1.team_agents import TeamAgent

FIELD = "request_task_ids"


def _member(*, requests: tuple[str, ...] = ("task_one", "task_two")) -> TeamAgent:
    return TeamAgent(agent="worker_1", agent_type="Researcher", display_name="Researcher-1", state="busy",
                     accepting_work=True, since=datetime(2026, 11, 20, 9, 0, tzinfo=timezone.utc), host="lead",
                     session="s-worker_1", is_lead=False, requests=requests)


def test_the_team_view_fills_the_ids_from_the_record_and_none_for_a_departed_member() -> None:
    assert record_status(_member(), "team_one").request_task_ids == ["task_one", "task_two"]
    assert record_status(_member(), "team_one", exiting=True).request_task_ids == []
    assert record_status(_member(requests=()), "team_one").request_task_ids == []


def test_no_dump_of_an_agent_status_carries_the_ids() -> None:
    status = AgentStatus(self_id="worker_1", named_agent_type="Researcher", name="Researcher-1", is_paused=False,
                         agent_role="worker", status_type=MessageMetaType.RESPONSE, request_task_ids=["task_one"])
    assert status.request_task_ids == ["task_one"]
    assert FIELD not in status.model_dump()
    assert FIELD not in status.model_dump(mode="json")
    assert FIELD not in json.loads(status.model_dump_json())
    assert FIELD in AGENT_STATUS_RUNTIME_ONLY_FIELDS


def test_the_team_page_and_activity_mesh_payload_has_no_request_ids() -> None:
    presence = GatewayTeamPresence(members=lambda: [record_status(_member(), "team_one")],
                                   want_details=lambda: None, list_agent_classes=list)
    payload = presence.list_web_chat_team_members(presence_only=True).model_dump(mode="json")
    assert payload["members"] and "task_one" not in json.dumps(payload)
    assert all(FIELD not in member for member in payload["members"])
