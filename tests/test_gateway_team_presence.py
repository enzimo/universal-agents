from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from assistant_gateway.team_presence import GatewayTeamPresence, configured_agent_classes
from custom_messages.contracts.control_v1 import AgentStatus
from custom_messages.enums import ControlType, MessageMetaType


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def now_utc(self) -> datetime:
        return self.now


def _governance(now: datetime, *, list_agent_classes=lambda: []) -> GatewayTeamPresence:
    return GatewayTeamPresence(
        team_id="team_test",
        clock=_Clock(now),
        stale_after_seconds=30.0,
        list_agent_classes=list_agent_classes,
        logger=logging.getLogger("test.gateway.team_presence"),
    )


def _status(
    agent_id: str,
    *,
    executing: bool = False,
    exiting: bool = False,
    skills: list[str] | None = None,
) -> AgentStatus:
    return AgentStatus(
        self_id=agent_id,
        team_id="team_test",
        named_agent_type="CodeWritingAgent",
        is_paused=False,
        is_executing=executing,
        has_execution_capacity=not executing,
        is_busy=executing,
        is_exiting=exiting,
        is_team_lead=False,
        name=agent_id,
        agent_role="coder",
        available_skills=skills or [],
        status_type=MessageMetaType.RESPONSE,
        info_type=ControlType.STATUS_REQUEST_BASIC,
    )


def test_team_presence_snapshot_classifies_and_redacts_live_agents() -> None:
    now = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
    governance = _governance(now)
    available = _status("available", skills=["prototype", "prototype", "review"])
    busy = _status("busy", executing=True)
    exiting = _status("exiting", exiting=True)
    stale = _status("stale")

    governance.record_agent_presence(available, now, "presence")
    governance.record_agent_presence(busy, now, "presence")
    governance.record_agent_presence(exiting, now, "presence")
    governance.record_agent_presence(stale, now - timedelta(seconds=45), "presence")

    members = {
        member.agent_id: member
        for member in governance.list_web_chat_team_members().members
    }

    assert members["available"].status.label == "Available"
    assert members["available"].available_skills == ("prototype", "review")
    assert members["busy"].status.label == "Busy"
    assert members["exiting"].status.label == "Exiting"
    assert members["stale"].status.label == "Stale"
    assert set(type(members["available"]).model_fields) == {
        "schema_version",
        "agent_id",
        "name",
        "named_agent_type",
        "is_team_lead",
        "status",
        "available_skills",
        "last_seen_at",
    }


def test_defined_classes_include_team_agent_definitions_without_exposing_prompts(tmp_path):
    from runtime_settings.instance_layout import InstanceLayout

    layout = InstanceLayout.at(tmp_path)
    layout.ensure_directories()
    definitions = layout.agent_definitions_dir
    definitions.mkdir()
    (definitions / "dormant.yaml").write_text("""
schema_version: 1
agent_types:
  DormantAgent:
    display_name: Dormant reviewer
    role: Reviewer
    description: Reviews saved reports.
    prompt: Do not expose this private prompt.
    include_shared_prompt: false
    default_tools: [fn:publishing]
""")
    config = SimpleNamespace(
        instance_root_path=layout.root, agent=SimpleNamespace(agent_definition_paths=[]),
    )
    classes = configured_agent_classes(config)
    custom = next(item for item in classes if item["named_agent_type"] == "DormantAgent")
    assert custom == {"named_agent_type": "DormantAgent", "display_name": "Dormant reviewer",
                      "description": "Reviews saved reports."}
    assert "private prompt" not in str(classes)
    governance = _governance(
        datetime(2026, 9, 21, tzinfo=timezone.utc), list_agent_classes=lambda: configured_agent_classes(config)
    )
    projection = governance.list_web_chat_team_members()
    assert projection.members == ()
    assert any(item.named_agent_type == "DormantAgent" for item in projection.definitions)
