from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from custom_messages.contracts.access_v2 import ChangeApprovalMode, ChangeApprovalRecordV1

from custom_agents.team_composition import (
    TEAM_COMPOSITION_SCHEMA_VERSION,
    RemovedRosterAttachments,
    TeamCompositionConfig,
    load_team_composition,
    write_generated_agent_bundle,
)
from runtime_settings.instance_layout import InstanceLayout


@pytest.mark.parametrize(
    "section",
    [
        {"agent_types": {}},
        {"agent_types": {"CodeWritingAgent": [{"capability_id": "builtin:learning_coaching"}]}},
    ],
)
def test_roster_with_the_removed_attachment_section_is_refused_with_the_repair(
    tmp_path: Path, section: dict
) -> None:
    """Attachments were removed; the roster is refused (not rewritten) and says what to do."""

    path = tmp_path / "TEAM_AGENTS.yaml"
    original = yaml.safe_dump({"schema_version": TEAM_COMPOSITION_SCHEMA_VERSION, "approved_attachments": section})
    path.write_text(original, encoding="utf-8")

    with pytest.raises(RemovedRosterAttachments) as refused:
        load_team_composition(path)

    message = str(refused.value)
    assert str(path) in message
    assert "Delete the approved_attachments section" in message
    assert "grants" in message
    if section["agent_types"]:
        # A non-empty section says what it gave, so the operator can give it again.
        assert "CodeWritingAgent: builtin:learning_coaching" in message
    else:
        assert "It is empty" in message and "the same agents start" in message
    assert path.read_text(encoding="utf-8") == original


def test_change_approval_record_refuses_a_request_id_for_agreement() -> None:
    with pytest.raises(ValidationError, match="Only a person's approval names an access request"):
        ChangeApprovalRecordV1(
            mode=ChangeApprovalMode.AGREEMENT,
            approved_by="agreement",
            approved_at_utc=datetime(2026, 7, 21, 12, tzinfo=timezone.utc),
            access_request_id="access_req_test",
        )


def test_generated_agent_bundle_requires_overwrite(tmp_path: Path) -> None:
    composition = TeamCompositionConfig()
    kwargs = {
        "control_dir": InstanceLayout.at(tmp_path).control_dir,
        "config": composition,
        "agent_type": "PlanningCriticAgent",
        "template_name": "TaskSpecialistTemplate",
        "params": {"display_name": "Planning Critic"},
    }

    write_generated_agent_bundle(**kwargs)

    with pytest.raises(FileExistsError):
        write_generated_agent_bundle(**kwargs)
