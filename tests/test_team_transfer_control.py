"""The ``control`` transfer section carries the team's control files, checked by this version's loaders."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from team_transfer.container import TransferReader
from team_transfer.control import ControlSection, ControlUnitV1
from team_transfer.crypto import new_key_derivation
from team_transfer.sections import RecordOutcome
from team_transfer.service import export_source, export_team, import_team
from team_transfer.settings import SettingsSection
from tests.pinned_memory_support import new_instance

PASSPHRASE = "correct horse battery staple"
FAST_KEYS = {"opslimit": 1, "memlimit": 8192}
SECTIONS = (SettingsSection(), ControlSection())

GOOD_BUNDLE = """schema_version: 1
agent_types:
  TravelPlanner:
    display_name: "Travel Planner"
    role: "Plans trips"
    description: "Plans trips for the team."
    prompt_ref: "prompt.md"
    default_tools:
      - "fn:schedules"
"""
# tool_capabilities was replaced by default_tools; this version refuses it.
OLD_FIELD_DEFINITION = """schema_version: 1
agent_types:
  OldStyle:
    display_name: "Old Style"
    role: "Uses a removed field"
    description: "Defined before default_tools."
    prompt_ref: "prompts/team_architect.md"
    tool_capabilities: ["learning_coaching"]
"""


def _old_instance(tmp_path: Path) -> tuple[Path, Any]:
    from assistant_gateway.bootstrap import bootstrap_instance

    path, config = new_instance(tmp_path, "alpha", "team_a")
    bootstrap_instance(config)
    control = config.layout.control_dir
    (control / "SOUL.md").write_text("# Assistant Identity\n\nName: Ada\nPurpose: Run the travel desk.\n")
    bundle = control / "agent_bundles" / "travel"
    bundle.mkdir(parents=True)
    (bundle / "agent.yaml").write_text(GOOD_BUNDLE)
    (bundle / "prompt.md").write_text("You plan trips.\n")
    (control / "agent_definitions").mkdir(exist_ok=True)
    (control / "agent_definitions" / "old.yaml").write_text(OLD_FIELD_DEFINITION)
    (control / "notes.txt").write_text("operator notes\n")
    # Owned by other sections; never carried here.
    (control / "skills" / "demo").mkdir(parents=True)
    (control / "skills" / "demo" / "SKILL.md").write_text("skill\n")
    _specialist(control, "Approved", team_id="team_a")
    tampered = _specialist(control, "Tampered", team_id="team_a")
    (tampered / "agent.yaml").write_text((tampered / "agent.yaml").read_text() + "# edited after approval\n")
    return path, config


def _specialist(control: Path, agent_type: str, *, team_id: str) -> Path:
    """A generated specialist as GeneratedAgentProvisioningService.apply leaves it."""

    import hashlib
    from datetime import datetime, timedelta, timezone

    from custom_agents.generated_agent_runtime import GeneratedAgentProvisioningRecordV1
    from custom_messages.contracts.access_v2 import ChangeApprovalMode, ChangeApprovalRecordV1

    bundle = control / "agent_bundles" / "team_lead_generated" / agent_type
    bundle.mkdir(parents=True)
    catalog = GOOD_BUNDLE.replace("TravelPlanner", agent_type).replace('prompt_ref: "prompt.md"', 'prompt_ref: "prompt.md"')
    (bundle / "agent.yaml").write_text(catalog)
    (bundle / "prompt.md").write_text("You plan trips.\n")
    now = datetime(2026, 9, 1, tzinfo=timezone.utc)
    record = GeneratedAgentProvisioningRecordV1(
        provisioning_id=f"generated_agent_provisioning_{agent_type.lower()}",
        change_id="generated_agent_0123456789abcdef0123",
        plan_digest="2" * 64,
        status="ready",
        agent_type=agent_type,
        team_id=team_id,
        owner_canonical_user_id="usr_owner",
        actor_principal_id="agent_lead",
        default_tools=("fn:schedules",),
        catalog_sha256=hashlib.sha256(catalog.encode()).hexdigest(),
        approval=ChangeApprovalRecordV1(
            mode=ChangeApprovalMode.AGREEMENT, approved_by="usr_owner", approved_at_utc=now + timedelta(seconds=1)
        ),
        created_at_utc=now,
        applied_at_utc=now + timedelta(seconds=1),
    )
    (bundle / "provisioning.json").write_text(record.model_dump_json())
    return bundle


def _export(tmp_path: Path, config_path: Path) -> Path:
    out = tmp_path / "team.uatransfer"
    export_team(
        export_source(config_path),
        out,
        PASSPHRASE,
        owner_person_id="usr_owner",
        sections=SECTIONS,
        key_derivation=new_key_derivation(**FAST_KEYS),
    )
    return out


def test_control_files_travel_as_units_and_other_sections_keep_their_parts(tmp_path: Path) -> None:
    path, _config = _old_instance(tmp_path)

    with TransferReader.open(_export(tmp_path, path), PASSPHRASE) as reader:
        document = reader.section_document("control")
    assert document is not None
    units = {ControlUnitV1.model_validate(record).unit: ControlUnitV1.model_validate(record) for record in document.records}

    assert {"SOUL.md", "TEAM_AGENTS.yaml", "TEAM_TEMPLATES.yaml"} <= set(units)
    assert "LLM_OVERRIDES.yaml" not in units, "bootstrap's unchanged defaults must not freeze the old release"
    assert sorted(entry.path for entry in units["agent_bundles/travel"].files) == [
        "agent_bundles/travel/agent.yaml",
        "agent_bundles/travel/prompt.md",
    ]
    assert not any(unit.startswith("skills") for unit in units)
    specialist = units["agent_bundles/team_lead_generated/Approved"]
    assert specialist.kind.value == "specialist"
    assert sorted(entry.path.rsplit("/", 1)[1] for entry in specialist.files) == [
        "agent.yaml",
        "prompt.md",
        "provisioning.json",
    ]


def test_import_writes_checked_units_and_refuses_what_this_version_refuses(tmp_path: Path) -> None:
    path, _config = _old_instance(tmp_path)
    out = _export(tmp_path, path)
    target = tmp_path / "new"

    with TransferReader.open(out, PASSPHRASE) as reader:
        plan = import_team(reader, target, dry_run=False, sections=SECTIONS)

    outcomes = {record.key: record for record in plan.records if record.section == "control"}
    assert outcomes["SOUL.md"].outcome is RecordOutcome.IMPORT
    assert outcomes["agent_bundles/travel"].outcome is RecordOutcome.IMPORT
    assert outcomes["agent_definitions/old.yaml"].outcome is RecordOutcome.REFUSED
    assert "tool_capabilities" in outcomes["agent_definitions/old.yaml"].reason
    assert outcomes["notes.txt"].outcome is RecordOutcome.ATTENTION
    assert outcomes["agent_bundles/team_lead_generated/Approved"].outcome is RecordOutcome.IMPORT
    tampered = outcomes["agent_bundles/team_lead_generated/Tampered"]
    assert tampered.outcome is RecordOutcome.REFUSED and "changed after the specialist was approved" in tampered.reason

    control = target / "control"
    assert "Run the travel desk." in (control / "SOUL.md").read_text()
    assert (control / "agent_bundles" / "travel" / "prompt.md").read_text() == "You plan trips.\n"
    assert not (control / "agent_definitions" / "old.yaml").exists()
    assert (control / "notes.txt").read_text() == "operator notes\n"
    assert not (control / "skills" / "demo").exists()
    generated = control / "agent_bundles" / "team_lead_generated"
    assert (generated / "Approved" / "provisioning.json").is_file()
    assert not (generated / "Tampered").exists()


def test_an_edited_control_file_is_refused_before_anything_is_written(tmp_path: Path) -> None:
    import zipfile

    import pytest

    from team_transfer.container import TransferFileError

    path, _config = _old_instance(tmp_path)
    out = _export(tmp_path, path)
    # Swap a carried file for another of the same size: the manifest digest catches it.
    with zipfile.ZipFile(out) as archive:
        entries = [(info.filename, archive.read(info.filename)) for info in archive.infolist()]
    out.unlink()
    with zipfile.ZipFile(out, "w") as archive:
        for name, data in entries:
            archive.writestr(name, data.replace(b"trips", b"TRIPS") if name.endswith("prompt.md") else data)

    with pytest.raises(TransferFileError):
        TransferReader.open(out, PASSPHRASE)
