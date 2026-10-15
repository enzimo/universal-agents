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
