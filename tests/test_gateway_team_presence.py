from __future__ import annotations

import logging
from datetime import datetime, timezone
from types import SimpleNamespace

from assistant_gateway.team_presence import GatewayTeamPresence, configured_agent_classes
from custom_messages.contracts.control_v1 import AgentStatus
from custom_messages.enums import ControlType, MessageMetaType


def _governance(statuses=(), *, list_agent_classes=lambda: [], asked: list | None = None) -> GatewayTeamPresence:
    return GatewayTeamPresence(
        members=lambda: list(statuses),
        want_details=(lambda: asked.append("details")) if asked is not None else (lambda: None),
        list_agent_classes=list_agent_classes,
        logger=logging.getLogger("test.gateway.team_presence"),
    )


def _status(
    agent_id: str,
    *,
    executing: bool = False,
    exiting: bool = False,
    paused: bool = False,
    accepting: bool = True,
    skills: list[str] | None = None,
    name: str | None = None,
) -> AgentStatus:
    return AgentStatus(
        self_id=agent_id,
        team_id="team_test",
        named_agent_type="CodeWritingAgent",
        is_paused=paused,
        is_executing=executing,
        is_busy=executing,
        is_exiting=exiting,
        accepting_work=accepting,
        is_team_lead=False,
        name=agent_id if name is None else name,
        agent_role="worker",
        available_skills=skills or [],
        last_seen_at=datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc).isoformat(),
        status_type=MessageMetaType.RESPONSE,
        info_type=ControlType.STATUS_REQUEST_BASIC,
    )


def test_team_presence_snapshot_classifies_and_redacts_live_agents() -> None:
    governance = _governance([
        _status("available", skills=["prototype", "prototype", "review"]),
        _status("busy", executing=True),
        _status("exiting", exiting=True),
        _status("paused", paused=True, accepting=False),
        _status("other_release", accepting=False),
        _status("other_release_busy", executing=True, accepting=False),
    ])

    members = {member.agent_id: member for member in governance.list_web_chat_team_members().members}

    assert members["available"].status.label == "Available"
    assert members["available"].available_skills == ("prototype", "review")
    assert members["busy"].status.label == "Busy"
    assert members["exiting"].status.label == "Exiting"
    assert members["paused"].status.label == "Paused"
    # A member that takes no new work (another contract, starting, winding down) says so, unless busy (review F22).
    assert (members["other_release"].status.key, members["other_release"].status.label,
            members["other_release"].status.tone) == ("not_accepting", "Not accepting work", "neutral")
    assert members["other_release_busy"].status.label == "Busy"
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


def test_only_a_full_team_read_asks_the_members_for_their_details() -> None:
    asked: list[str] = []
    governance = _governance([_status("worker")], asked=asked)
    governance.list_web_chat_team_members(presence_only=True)
    assert asked == []
    governance.list_web_chat_team_members()
    assert asked == ["details"]


def test_one_member_that_does_not_fit_the_page_is_logged_and_left_out(caplog) -> None:
    """Review F18: everyone's live page and Tasks read this list, so one bad member must not break it."""

    governance = _governance([_status("worker"), _status("blank", name="   ")])
    with caplog.at_level(logging.WARNING, logger="test.gateway.team_presence"):
        members = governance.list_web_chat_team_members(presence_only=True).members
    assert [member.agent_id for member in members] == ["worker"]
    assert "event=gateway_team_member_invalid agent_id=blank" in caplog.text


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
    governance = _governance(list_agent_classes=lambda: configured_agent_classes(config))
    projection = governance.list_web_chat_team_members()
    assert projection.members == ()
    assert any(item.named_agent_type == "DormantAgent" for item in projection.definitions)
