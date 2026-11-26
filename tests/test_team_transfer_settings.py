"""The ``settings`` transfer section carries a team's configuration choices to a fresh instance.

Each test saves a real ``instance_config.yaml``, exports it with the real
section, and imports into a fresh root through the real import path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from internal_credentials import parse_env_file
from team_transfer.container import TransferReader
from team_transfer.crypto import new_key_derivation
from team_transfer.sections import ExportSource, RecordOutcome, SectionExport, SectionFiles
from team_transfer.service import TransferError, export_source, export_team, import_team
from team_transfer.settings import INSTANCE_ROOT_TOKEN, SettingsSection, SettingV1
from tests.pinned_memory_support import new_instance

PASSPHRASE = "correct horse battery staple"
FAST_KEYS = {"opslimit": 1, "memlimit": 8192}


def _old_instance(tmp_path: Path, change=None) -> tuple[Path, Any]:
    path, config = new_instance(tmp_path, "alpha", "team_a")
    if change is not None:
        change(config)
        config.save(path)
    return path, config


def _export(tmp_path: Path, config_path: Path, section: Any = None) -> Path:
    out = tmp_path / "team.uatransfer"
    export_team(
        export_source(config_path),
        out,
        PASSPHRASE,
        owner_person_id="usr_owner",
        sections=(section or SettingsSection(),),
        key_derivation=new_key_derivation(**FAST_KEYS),
    )
    return out


def _import(out: Path, target: Path, *, dry_run: bool):
    with TransferReader.open(out, PASSPHRASE) as reader:
        return import_team(reader, target, dry_run=dry_run, sections=(SettingsSection(),))


def _records(out: Path) -> list[SettingV1]:
    with TransferReader.open(out, PASSPHRASE) as reader:
        document = reader.section_document("settings")
    assert document is not None
    return [SettingV1.model_validate(record) for record in document.records]


def _team_choices(config: Any) -> None:
    config.heartbeat.enabled = False
    config.user.timezone = "Europe/Lisbon"
    config.llm.model_id = "anthropic/claude-opus-5-5"
    config.launch.env_file_path = str(config.instance_root_path / "team.env")


def test_a_default_instance_carries_no_settings(tmp_path: Path) -> None:
    path, _config = _old_instance(tmp_path)

    assert _records(_export(tmp_path, path)) == []


def test_only_changed_settings_are_carried_with_paths_relative_to_the_root(tmp_path: Path) -> None:
    path, _config = _old_instance(tmp_path, _team_choices)

    carried = {record.dotted: record.value for record in _records(_export(tmp_path, path))}

    assert carried == {
        "heartbeat.enabled": False,
        "user.timezone": "Europe/Lisbon",
        "llm.model_id": "anthropic/claude-opus-5-5",
        "launch.env_file_path": f"{INSTANCE_ROOT_TOKEN}/team.env",
    }


def test_import_builds_the_instance_like_onboarding_with_the_team_choices(tmp_path: Path) -> None:
    from assistant_gateway.config import GatewayInstanceConfig

    path, _config = _old_instance(tmp_path, _team_choices)
    out = _export(tmp_path, path)
    target = tmp_path / "new"

    dry = _import(out, target, dry_run=True)
    assert {(r.key, r.outcome) for r in dry.records} == {
        ("heartbeat.enabled", RecordOutcome.IMPORT),
        ("user.timezone", RecordOutcome.IMPORT),
        ("llm.model_id", RecordOutcome.IMPORT),
        ("launch.env_file_path", RecordOutcome.IMPORT),
    }
    assert not target.exists()

    _import(out, target, dry_run=False)

    imported = GatewayInstanceConfig.load(target / "instance_config.yaml")
    assert (imported.instance_name, imported.gateway.team_id) == ("alpha", "team_a")
    assert imported.instance_root_path == target.resolve()
    assert imported.heartbeat.enabled is False and imported.user.timezone == "Europe/Lisbon"
    assert imported.llm.model_id == "anthropic/claude-opus-5-5"
    assert imported.launch.env_file_path == str(target.resolve() / "team.env")
    # Unchanged settings take this version's defaults, not copies of the old ones.
    assert imported.agent.model_dump() == GatewayInstanceConfig.create(
        instance_name="alpha", instance_root=str(target), team_id="team_a", timezone="UTC"
    ).agent.model_dump()
    # The same first steps as onboarding: folders, control files, fresh keys.
    assert (target / "control" / "SOUL.md").is_file()
    env = parse_env_file(target / "team.env")
    assert env["ACCESS_GATEWAY_SIGNING_KEY"] and env["SECURE_TOOL_BROKER_TOKEN"].startswith("stb_")


def test_a_setting_this_version_does_not_accept_is_refused_and_the_rest_apply(tmp_path: Path) -> None:
    from assistant_gateway.config import GatewayInstanceConfig

    class Crafted(SettingsSection):
        def export(self, source: ExportSource, files: SectionFiles) -> SectionExport:
            records = [
                SettingV1(path=("heartbeat", "enabled"), value=False),
                SettingV1(path=("agent", "setting_removed_long_ago"), value=3),
                SettingV1(path=("heartbeat", "interval_seconds"), value="not a number"),
                SettingV1(path=("llm", "model_id", "nested"), value="x"),
            ]
            return SectionExport(records=[record.model_dump(mode="json") for record in records])

    path, _config = _old_instance(tmp_path)
    out = _export(tmp_path, path, Crafted())
    target = tmp_path / "new"

    plan = _import(out, target, dry_run=False)

    outcomes = {record.key: record for record in plan.records}
    assert outcomes["heartbeat.enabled"].outcome is RecordOutcome.IMPORT
    for refused in ("agent.setting_removed_long_ago", "heartbeat.interval_seconds", "llm.model_id.nested"):
        assert outcomes[refused].outcome is RecordOutcome.REFUSED, refused
    assert "no setting group llm.model_id" in outcomes["llm.model_id.nested"].reason
    imported = GatewayInstanceConfig.load(target / "instance_config.yaml")
    assert imported.heartbeat.enabled is False
    assert imported.heartbeat.interval_seconds == GatewayInstanceConfig.create(
        instance_name="alpha", instance_root=str(target), team_id="team_a", timezone="UTC"
    ).heartbeat.interval_seconds


@pytest.mark.parametrize("exported_version", [5, 6, 7, 8])
def test_a_previous_export_keeps_its_values_and_the_new_settings_keep_their_defaults(
    tmp_path: Path, exported_version: int
) -> None:
    """Older choices survive while settings newer than the export use defaults.

    Version 8 predates the skill semantic-review settings, so its import keeps
    the review turned off even when the team changed another skill
    installation setting.
    """

    from assistant_gateway.config import GatewayInstanceConfig

    class ExportedAsPreviousVersion(SettingsSection):
        schema_version = exported_version

        def export(self, source: ExportSource, files: SectionFiles) -> SectionExport:
            records = [
                SettingV1(path=("heartbeat", "enabled"), value=False),
                SettingV1(path=("agent", "max_queued_top_level_tasks"), value=22),
            ]
            if exported_version == 7:
                records.append(SettingV1(path=("tool_policy", "output_retention_days"), value=0))
            if exported_version == 8:
                records.append(SettingV1(path=("workspace", "skill_installation", "safe_max_score"), value=10))
            return SectionExport(records=[record.model_dump(mode="json") for record in records])

    path, _config = _old_instance(tmp_path)
    out = _export(tmp_path, path, ExportedAsPreviousVersion())
    target = tmp_path / "new"

    plan = _import(out, target, dry_run=False)

    outcomes = {record.key: record for record in plan.records}
    assert outcomes["heartbeat.enabled"].outcome is RecordOutcome.IMPORT
    assert outcomes["agent.max_queued_top_level_tasks"].outcome is RecordOutcome.IMPORT
    imported = GatewayInstanceConfig.load(target / "instance_config.yaml")
    assert imported.heartbeat.enabled is False
    assert imported.agent.max_queued_top_level_tasks == 22
    assert imported.agent.request_conflict_checks is True
    assert imported.tool_policy.output_retention_days == (0 if exported_version == 7 else 90)
    assert imported.gateway.storage_warnings.model_dump() == {
        "person_step_gb": 5,
        "nearing_limit_percent": 80,
        "nearly_full_percent": 95,
    }
    skill_installation = imported.workspace.skill_installation
    assert skill_installation.safe_max_score == (10 if exported_version == 8 else 20)
    assert skill_installation.semantic_review.model_dump() == {"mode": "off", "max_input_bytes": 262144}


def test_a_selected_skill_semantic_review_moves_with_the_team(tmp_path: Path) -> None:
    """This version's export carries the skill semantic-review choice and its input limit."""

    from assistant_gateway.config import GatewayInstanceConfig

    def selected_review(config: Any) -> None:
        config.workspace.skill_installation.semantic_review.mode = "task_model"
        config.workspace.skill_installation.semantic_review.max_input_bytes = 524288

    path, _config = _old_instance(tmp_path, selected_review)
    out = _export(tmp_path, path)
    assert {record.dotted: record.value for record in _records(out)} == {
        "workspace.skill_installation.semantic_review.mode": "task_model",
        "workspace.skill_installation.semantic_review.max_input_bytes": 524288,
    }
    target = tmp_path / "new"

    plan = _import(out, target, dry_run=False)

    assert {(r.key, r.outcome) for r in plan.records} == {
        ("workspace.skill_installation.semantic_review.mode", RecordOutcome.IMPORT),
        ("workspace.skill_installation.semantic_review.max_input_bytes", RecordOutcome.IMPORT),
    }
    imported = GatewayInstanceConfig.load(target / "instance_config.yaml")
    assert imported.workspace.skill_installation.semantic_review.model_dump() == {
        "mode": "task_model",
        "max_input_bytes": 524288,
    }


def test_a_path_outside_the_instance_that_this_host_lacks_needs_attention(tmp_path: Path) -> None:
    class Crafted(SettingsSection):
        def export(self, source: ExportSource, files: SectionFiles) -> SectionExport:
            record = SettingV1(path=("launch", "env_file_path"), value="/nowhere/on/this/host/team.env")
            return SectionExport(records=[record.model_dump(mode="json")])

    path, _config = _old_instance(tmp_path)
    plan = _import(_export(tmp_path, path, Crafted()), tmp_path / "new", dry_run=True)

    assert plan.records[0].outcome is RecordOutcome.ATTENTION
    assert "/nowhere/on/this/host/team.env" in plan.records[0].reason


def test_a_postgres_team_needs_the_new_database_before_import(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def postgres(config: Any) -> None:
        document = config.to_yaml_dict()
        document["state"]["storage"]["backend"] = "postgres"
        document["gateway"]["storage"]["backend"] = "postgres"
        (config.instance_root_path / "instance_config.yaml").write_text(yaml.safe_dump(document))

    path, config = _old_instance(tmp_path)
    postgres(config)
    out = _export(tmp_path, path)
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    monkeypatch.delenv("POSTGRES_DSN_FILE", raising=False)

    with pytest.raises(TransferError, match="put its connection string in POSTGRES_DSN"):
        _import(out, tmp_path / "new", dry_run=True)

    target = tmp_path / "with-dsn"
    target.mkdir()
    (target / "team.env").write_text("POSTGRES_DSN=postgresql://u@localhost/new_empty\n")
    plan = _import(out, target, dry_run=True)
    assert {record.key for record in plan.records} >= {"state.storage.backend", "gateway.storage.backend"}


@pytest.mark.parametrize("chosen_name", [None, "Assistant", "Team helper"])
def test_carried_names_are_kept_and_an_omitted_name_takes_this_versions_default(tmp_path, chosen_name):
    from assistant_gateway.config import GatewayInstanceConfig

    class Carried(SettingsSection):
        def export(self, source, files):
            # An export omits unchanged defaults, including the default name.
            records = [SettingV1(path=("heartbeat", "enabled"), value=False)]
            if chosen_name is not None:
                records.append(SettingV1(path=("assistant", "display_name"), value=chosen_name))
            return SectionExport(records=[row.model_dump(mode="json") for row in records])

    path, _config = _old_instance(tmp_path)
    archive, target = _export(tmp_path, path, Carried()), tmp_path / "new"
    _import(archive, target, dry_run=False)
    imported = GatewayInstanceConfig.load_for_process(target / "instance_config.yaml")
    assert imported.assistant.display_name == (chosen_name or "Lisa")
    assert not imported.heartbeat.enabled


def _environment_secrets(config: Any) -> None:
    config.launch.secrets_source = "environment"
    config.launch.env_file_path = None


def test_the_destination_secret_source_replaces_the_carried_one(tmp_path: Path) -> None:
    """Onboarding's Import asks where secrets come from; the answer wins over the export's."""

    from team_transfer.sections import DestinationChoices

    path, _config = _old_instance(tmp_path, _environment_secrets)
    out = _export(tmp_path, path)
    target = tmp_path / "new"
    with TransferReader.open(out, PASSPHRASE) as reader:
        plan = import_team(
            reader,
            target,
            dry_run=False,
            sections=(SettingsSection(),),
            destination=DestinationChoices(secrets_source="env_file"),
        )

    [carried] = [record for record in plan.records if record.key == "launch.secrets_source"]
    assert carried.outcome is RecordOutcome.ATTENTION
    assert carried.reason == "replaced by the secret source chosen for this instance: env_file"
    saved = yaml.safe_load((target / "instance_config.yaml").read_text())
    assert saved["launch"]["secrets_source"] == "env_file"
    assert "SECURE_TOOL_BROKER_TOKEN" in parse_env_file(target / "team.env")


def test_choosing_environment_secrets_writes_no_team_env(tmp_path: Path) -> None:
    from team_transfer.sections import DestinationChoices

    path, _config = _old_instance(tmp_path)
    out = _export(tmp_path, path)
    target = tmp_path / "new"
    with TransferReader.open(out, PASSPHRASE) as reader:
        import_team(
            reader,
            target,
            dry_run=False,
            sections=(SettingsSection(),),
            destination=DestinationChoices(secrets_source="environment"),
        )

    saved = yaml.safe_load((target / "instance_config.yaml").read_text())
    assert saved["launch"]["secrets_source"] == "environment"
    assert not saved["launch"].get("env_file_path")
    assert not (target / "team.env").exists()


def test_the_wait_limits_stay_behind_with_a_warning_and_a_note_in_the_report(tmp_path: Path, caplog) -> None:
    """This line does not carry agent.max_wait_minutes_* (settings stays at version 9, which release/0.10 shares).

    A changed limit is left out of the export; the export logs it and its
    report names it, so the operator sets it again after the move. The rest of
    the team's choices still move.
    """

    from assistant_gateway.config import GatewayInstanceConfig

    def shorter_waits(config: Any) -> None:
        config.agent.max_wait_minutes_without_approval = 10
        config.agent.max_wait_minutes_with_approval = 120
        config.heartbeat.enabled = False

    path, _config = _old_instance(tmp_path, shorter_waits)
    out = tmp_path / "team.uatransfer"
    with caplog.at_level("WARNING", logger="team_transfer.settings"):
        report = export_team(
            export_source(path), out, PASSPHRASE, owner_person_id="usr_owner", sections=(SettingsSection(),),
            key_derivation=new_key_derivation(**FAST_KEYS),
        )

    assert {record.dotted: record.value for record in _records(out)} == {"heartbeat.enabled": False}
    assert sorted(note.split(" here,")[0] for note in report.notes) == [
        "agent.max_wait_minutes_with_approval is 120",
        "agent.max_wait_minutes_without_approval is 10",
    ]
    assert all("set it again in the new instance's instance_config.yaml" in note for note in report.notes)
    assert caplog.text.count("event=team_transfer_setting_not_carried") == 2
    target = tmp_path / "new"
    _import(out, target, dry_run=False)
    imported = GatewayInstanceConfig.load(target / "instance_config.yaml")
    assert imported.heartbeat.enabled is False
    assert (imported.agent.max_wait_minutes_without_approval, imported.agent.max_wait_minutes_with_approval) == (20, 1440)


def test_default_wait_limits_add_no_note(tmp_path: Path) -> None:
    path, _config = _old_instance(tmp_path)
    report = export_team(
        export_source(path), tmp_path / "team.uatransfer", PASSPHRASE, owner_person_id="usr_owner",
        sections=(SettingsSection(),), key_derivation=new_key_derivation(**FAST_KEYS),
    )

    assert report.notes == ()


def test_the_settings_contract_is_main_and_release_0_10s_settings_9() -> None:
    """settings/9 means the same format on main and release/0.10, so this line's contract must not move.

    The section's payload contract leaves out the settings it does not carry;
    its fingerprint and the tracked schema's checksum are pinned to their values
    on origin/main (and release/0.10).
    """

    import json

    from scripts.export_team_transfer_schemas import DEFAULT_OUTPUT_DIR, section_contract_digests
    from team_transfer.settings import NOT_CARRIED_SETTINGS, settings_payload_contract

    contract = settings_payload_contract()
    agent_properties = contract.model_json_schema()["$defs"]["AgentRuntimeDefaultsSettings"]["properties"]
    assert {path[-1] for path in NOT_CARRIED_SETTINGS} == {
        "max_wait_minutes_without_approval", "max_wait_minutes_with_approval",
    }
    assert not {path[-1] for path in NOT_CARRIED_SETTINGS} & set(agent_properties)
    assert "max_tool_calls_per_task" in agent_properties
    assert contract.__name__ == "GatewayInstanceConfig"
    pin_hint = (
        "main shares settings/9 with release/0.10 under the documented main-line exception "
        "(README_ARCHITECTURE.md, 'Moving a team to a new version'); add a new main-line setting "
        "to team_transfer.settings.NOT_CARRIED_SETTINGS instead of changing settings/9."
    )
    assert SettingsSection.schema_version == 9, pin_hint
    assert section_contract_digests()["settings/9"] == (
        "791cfc3cbb6e62404c85c0e5d53f2c9b9183d1c46f719a7e574ade7996344e74"
    ), pin_hint
    manifest = json.loads((DEFAULT_OUTPUT_DIR / "manifest.json").read_text())
    assert [entry["sha256"] for entry in manifest["schemas"] if entry["model"] == "GatewayInstanceConfig"] == [
        "54020b8d38e62320daf64b7d8ed359b3f1334bb3387cdbf778893f8601f5b751"
    ], pin_hint
