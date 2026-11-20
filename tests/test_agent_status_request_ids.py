"""Request ids on AgentStatus never leave the gateway (Web Chat agent trail, plan review C08).

The gateway's team view sets ``AgentStatus.request_task_ids`` from each agent's
signed record so the thread list can say who works on a person's request. The
ids name one person's requests, so they are a private attribute: no message can
set them (C12), and no dump or schema carries them: not a status reply, not a
model's view, not the Team page or the activity mesh (C08).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from assistant_gateway.team_presence import GatewayTeamPresence
from assistant_gateway.team_view import record_status
from custom_messages.contracts.control_v1 import AgentStatus
from custom_messages.enums import MessageMetaType
from custom_transport.protocol1.team_agents import TeamAgent

FIELD = "request_task_ids"


def _member(*, requests: tuple[str, ...] = ("task_one", "task_two")) -> TeamAgent:
    return TeamAgent(agent="worker_1", agent_type="Researcher", display_name="Researcher-1", state="busy",
                     accepting_work=True, since=datetime(2026, 11, 20, 9, 0, tzinfo=timezone.utc), host="lead",
                     session="s-worker_1", is_lead=False, requests=requests)


def test_the_team_view_fills_the_ids_from_the_record_and_none_for_a_departed_member() -> None:
    assert record_status(_member(), "team_one").request_task_ids == ("task_one", "task_two")
    assert record_status(_member(), "team_one", exiting=True).request_task_ids == ()
    assert record_status(_member(requests=()), "team_one").request_task_ids == ()


def _status(**fields: object) -> AgentStatus:
    return AgentStatus(self_id="worker_1", named_agent_type="Researcher", name="Researcher-1", is_paused=False,
                       agent_role="worker", status_type=MessageMetaType.RESPONSE, **fields)


def test_no_dump_or_schema_of_an_agent_status_carries_the_ids() -> None:
    status = _status().with_request_task_ids(("task_one",))
    assert status.request_task_ids == ("task_one",)
    assert FIELD not in status.model_dump()
    assert FIELD not in status.model_dump(mode="json")
    assert "task_one" not in status.model_dump_json()
    assert FIELD not in json.dumps(AgentStatus.model_json_schema())
    # A copy (the team view keeps departed members' statuses) keeps them.
    assert status.model_copy().request_task_ids == ("task_one",)


def test_a_status_read_from_a_message_never_carries_ids() -> None:
    raw = json.loads(_status().model_dump_json())
    raw[FIELD] = ["task_forged"]
    assert AgentStatus.model_validate(raw).request_task_ids == ()
    assert AgentStatus.model_validate_json(json.dumps(raw)).request_task_ids == ()


def test_the_team_page_and_activity_mesh_payload_has_no_request_ids() -> None:
    presence = GatewayTeamPresence(members=lambda: [record_status(_member(), "team_one")],
                                   want_details=lambda: None, list_agent_classes=list)
    payload = presence.list_web_chat_team_members(presence_only=True).model_dump(mode="json")
    assert payload["members"] and "task_one" not in json.dumps(payload)
    assert all(FIELD not in member for member in payload["members"])
