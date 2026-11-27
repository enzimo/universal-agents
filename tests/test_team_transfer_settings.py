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
from team_transfer.sections import SECTION_UPGRADES, ExportSource, RecordOutcome, SectionExport, SectionFiles
from team_transfer.service import TransferError, export_source, export_team, import_team
from team_transfer.settings import INSTANCE_ROOT_TOKEN, SettingsSection, SettingV1
from default_values import MIB
from runtime_settings.transport import PARTITION_COUNT_SETTING, QueueSettings
from tests.pinned_memory_support import new_instance

PASSPHRASE = "correct horse battery staple"
FAST_KEYS = {"opslimit": 1, "memlimit": 8192}
SEMANTIC_REVIEW = ("workspace", "skill_installation", "semantic_review")


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
    assert "ACCESS_GATEWAY_SIGNING_KEY" not in env and env["SECURE_TOOL_BROKER_TOKEN"].startswith("stb_")


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
            records.append(SettingV1(path=("agent", "max_concurrent_top_level_tasks"), value=4))
            return SectionExport(records=[record.model_dump(mode="json") for record in records])

    path, _config = _old_instance(tmp_path)
    out = _export(tmp_path, path, Crafted())
    target = tmp_path / "new"

    plan = _import(out, target, dry_run=False)

    outcomes = {record.key: record for record in plan.records}
    assert outcomes["heartbeat.enabled"].outcome is RecordOutcome.IMPORT
    for refused in ("agent.setting_removed_long_ago", "heartbeat.interval_seconds", "llm.model_id.nested",
                    "agent.max_concurrent_top_level_tasks"):
        assert outcomes[refused].outcome is RecordOutcome.REFUSED, refused
    assert "no setting group llm.model_id" in outcomes["llm.model_id.nested"].reason
    imported = GatewayInstanceConfig.load(target / "instance_config.yaml")
    assert imported.heartbeat.enabled is False
    assert imported.heartbeat.interval_seconds == GatewayInstanceConfig.create(
        instance_name="alpha", instance_root=str(target), team_id="team_a", timezone="UTC"
    ).heartbeat.interval_seconds


@pytest.mark.parametrize("exported_version", [5, 6, 7, 8, 9, 10, 11, 12])
def test_a_previous_export_keeps_its_values_and_the_new_settings_keep_their_defaults(
    tmp_path: Path, exported_version: int
) -> None:
    """Older choices survive while settings an export predates use defaults.

    Versions 8 and 9 are what the releases before message contract step 7d export (9 is
    main's, from the 0.9 releases that added the skill semantic-review settings). They carry
    the presence stream's setting, which this version no longer has: the conversion drops
    it, so nothing is refused. Version 8 predates the semantic-review settings, so its
    import keeps the review turned off beside the team's other skill installation choices.
    Version 10 is this branch's pre-release export after step 7d. Every earlier version
    may still carry the ignored agent.max_concurrent_top_level_tasks, which version 11
    removed, so the conversion drops it too. Version 11 predates transport.queues, so the
    imported instance takes their defaults. Every earlier version read transport.replicas 0 (and
    "0") as one copy, which version 12 refuses: the conversion writes it as 1.
    """

    from assistant_gateway.config import GatewayInstanceConfig

    class ExportedAsPreviousVersion(SettingsSection):
        schema_version = exported_version

        def export(self, source: ExportSource, files: SectionFiles) -> SectionExport:
            records = [
                SettingV1(path=("heartbeat", "enabled"), value=False),
                SettingV1(path=("agent", "max_queued_top_level_tasks"), value=22),
            ]
            if exported_version >= 7:
                records.append(SettingV1(path=("tool_policy", "output_retention_days"), value=0))
            if exported_version >= 8:
                records.append(SettingV1(path=("gateway", "storage_warnings", "person_step_gb"), value=7))
                records.append(SettingV1(path=("workspace", "skill_installation", "safe_max_score"), value=10))
            if exported_version in (8, 9):
                records.append(SettingV1(path=("transport", "presence_max_age_seconds"), value=900.0))
            if exported_version >= 9:
                records.append(SettingV1(path=SEMANTIC_REVIEW + ("mode",), value="task_model"))
                records.append(SettingV1(path=SEMANTIC_REVIEW + ("max_input_bytes",), value=524288))
            records.append(SettingV1(path=("agent", "max_concurrent_top_level_tasks"), value=4))
            # Read as one copy before 0.10; a quoted 0 too, which 0.9's int field read (DI-1 of PR 152's review).
            records.append(SettingV1(path=("transport", "replicas"), value="0" if exported_version % 2 else 0))
            return SectionExport(records=[record.model_dump(mode="json") for record in records])

    path, _config = _old_instance(tmp_path)
    out = _export(tmp_path, path, ExportedAsPreviousVersion())
    target = tmp_path / "new"

    plan = _import(out, target, dry_run=False)

    outcomes = {record.key: record for record in plan.records}
    assert outcomes["heartbeat.enabled"].outcome is RecordOutcome.IMPORT
    assert outcomes["agent.max_queued_top_level_tasks"].outcome is RecordOutcome.IMPORT
    assert "transport.presence_max_age_seconds" not in outcomes
    assert "agent.max_concurrent_top_level_tasks" not in outcomes
    assert not plan.by_outcome(RecordOutcome.REFUSED)
    imported = GatewayInstanceConfig.load(target / "instance_config.yaml")
    assert imported.heartbeat.enabled is False
    assert imported.agent.max_queued_top_level_tasks == 22
    assert imported.agent.request_conflict_checks is True
    assert imported.tool_policy.output_retention_days == (0 if exported_version >= 7 else 90)
    assert imported.gateway.storage_warnings.model_dump() == {
        "person_step_gb": 7 if exported_version >= 8 else 5,
        "nearing_limit_percent": 80,
        "nearly_full_percent": 95,
    }
    skill_installation = imported.workspace.skill_installation
    assert skill_installation.safe_max_score == (10 if exported_version >= 8 else 20)
    assert skill_installation.semantic_review.model_dump() == (
        {"mode": "task_model", "max_input_bytes": 524288}
        if exported_version >= 9
        else {"mode": "off", "max_input_bytes": 262144}
    )
    assert imported.transport.queues == QueueSettings()
    assert outcomes["transport.replicas"].outcome is RecordOutcome.IMPORT and imported.transport.replicas == 1
    from default_values import DEFAULT_API_REQUEST_USER_AGENT

    assert imported.security_layer.api_request_user_agent == DEFAULT_API_REQUEST_USER_AGENT


def test_api_request_user_agent_moves_with_the_team(tmp_path: Path) -> None:
    import yaml

    from assistant_gateway.config import GatewayInstanceConfig

    # Default (unset) instances do not write api_request_user_agent or export it as a team choice.
    default_root = tmp_path / "default_inst"
    default_path, _default_cfg = _old_instance(default_root)
    raw_default = yaml.safe_load(default_path.read_text(encoding="utf-8"))
    assert "api_request_user_agent" not in raw_default["security_layer"]
    default_out = _export(default_root, default_path)
    assert "security_layer.api_request_user_agent" not in {record.dotted for record in _records(default_out)}

    # An explicit override is saved and carried by the transfer.
    def custom_ua(config: Any) -> None:
        config.security_layer.api_request_user_agent = "CustomTeamUA/1.0"

    path, _config = _old_instance(tmp_path, custom_ua)
    raw_custom = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert raw_custom["security_layer"]["api_request_user_agent"] == "CustomTeamUA/1.0"
    out = _export(tmp_path, path)
    assert {record.dotted: record.value for record in _records(out)} == {
        "security_layer.api_request_user_agent": "CustomTeamUA/1.0",
    }
    target = tmp_path / "new"
    plan = _import(out, target, dry_run=False)
    assert not plan.by_outcome(RecordOutcome.REFUSED)
    imported = GatewayInstanceConfig.load(target / "instance_config.yaml")
    assert imported.security_layer.api_request_user_agent == "CustomTeamUA/1.0"


def _queue_choices(config: Any) -> None:
    config.transport.queues.max_bytes.partition = 32 * MIB
    config.transport.queues.max_bytes.dead_letter = 64 * MIB
    config.transport.queues.warn_at_percent = 70
    config.transport.queues.max_messages_per_subject = 4
    config.transport.queues.partitions = 64


def test_queue_choices_move_with_the_team_but_the_partition_count_stays(tmp_path: Path) -> None:
    """The partition count is fixed when an instance is created, so the new instance keeps its own;
    the stream sizes, the warning level and the per-subject cap are the team's choices and travel."""

    from assistant_gateway.config import GatewayInstanceConfig

    path, _config = _old_instance(tmp_path, _queue_choices)
    out = _export(tmp_path, path)

    assert {record.dotted: record.value for record in _records(out)} == {
        "transport.queues.max_bytes.partition": 32 * MIB,
        "transport.queues.max_bytes.dead_letter": 64 * MIB,
        "transport.queues.warn_at_percent": 70,
        "transport.queues.max_messages_per_subject": 4,
    }
    target = tmp_path / "new"
    plan = _import(out, target, dry_run=False)

    assert not plan.by_outcome(RecordOutcome.REFUSED)
    queues = GatewayInstanceConfig.load(target / "instance_config.yaml").transport.queues
    assert (queues.max_bytes.partition, queues.max_bytes.dead_letter) == (32 * MIB, 64 * MIB)
    assert (queues.warn_at_percent, queues.max_messages_per_subject) == (70, 4)
    assert queues.partitions == QueueSettings().partitions


def test_a_carried_partition_count_is_refused_with_its_reason(tmp_path: Path) -> None:
    from assistant_gateway.config import GatewayInstanceConfig

    class Crafted(SettingsSection):
        def export(self, source: ExportSource, files: SectionFiles) -> SectionExport:
            records = [
                SettingV1(path=PARTITION_COUNT_SETTING, value=64),
                SettingV1(path=("transport", "queues", "warn_at_percent"), value=70),
            ]
            return SectionExport(records=[record.model_dump(mode="json") for record in records])

    path, _config = _old_instance(tmp_path)
    target = tmp_path / "new"

    plan = _import(_export(tmp_path, path, Crafted()), target, dry_run=False)

    outcomes = {record.key: record for record in plan.records}
    assert outcomes["transport.queues.partitions"].outcome is RecordOutcome.REFUSED
    assert "fixed when an instance is created" in outcomes["transport.queues.partitions"].reason
    assert outcomes["transport.queues.warn_at_percent"].outcome is RecordOutcome.IMPORT
    queues = GatewayInstanceConfig.load(target / "instance_config.yaml").transport.queues
    assert (queues.partitions, queues.warn_at_percent) == (QueueSettings().partitions, 70)


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


def test_this_branchs_pre_release_settings_version_9_imports_unchanged(tmp_path: Path) -> None:
    """Pre-releases of this branch exported their own settings version 9.

    It had neither the presence setting nor the skill semantic-review settings that main's
    version 9 carries, so the one step from 9 reads it without changing a record.
    """

    from assistant_gateway.config import GatewayInstanceConfig

    records = [
        SettingV1(path=("heartbeat", "enabled"), value=False),
        SettingV1(path=("tool_policy", "output_retention_days"), value=0),
        SettingV1(path=("gateway", "storage_warnings", "person_step_gb"), value=7),
    ]

    class ExportedAsPreRelease(SettingsSection):
        schema_version = 9

        def export(self, source: ExportSource, files: SectionFiles) -> SectionExport:
            return SectionExport(records=[record.model_dump(mode="json") for record in records])

    dumped = [record.model_dump(mode="json") for record in records]
    assert SECTION_UPGRADES[(SettingsSection.name, 9, SettingsSection.schema_version)](dumped) == dumped
    path, _config = _old_instance(tmp_path)
    target = tmp_path / "new"

    plan = _import(_export(tmp_path, path, ExportedAsPreRelease()), target, dry_run=False)

    assert {(r.key, r.outcome) for r in plan.records} == {
        ("heartbeat.enabled", RecordOutcome.IMPORT),
        ("tool_policy.output_retention_days", RecordOutcome.IMPORT),
        ("gateway.storage_warnings.person_step_gb", RecordOutcome.IMPORT),
    }
    imported = GatewayInstanceConfig.load(target / "instance_config.yaml")
    assert imported.heartbeat.enabled is False
    assert imported.tool_policy.output_retention_days == 0
    assert imported.gateway.storage_warnings.person_step_gb == 7
    assert imported.workspace.skill_installation.semantic_review.model_dump() == {
        "mode": "off",
        "max_input_bytes": 262144,
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


@pytest.mark.parametrize("upgraded_first", [True, False])
def test_a_0_9_instance_exports_no_retired_setting_and_a_reviewed_import_refuses_nothing(
    tmp_path: Path, upgraded_first: bool
) -> None:
    """A 0.9 instance holds transport.presence_max_age_seconds (message contract step 7d removed it)
    and agent.max_concurrent_top_level_tasks (0.9.4 onboarding wrote it; 0.10 removed it).

    Upgraded first: the launcher rewrote the file at its first start. Not upgraded: the export
    ran after the code was upgraded, before any start; the export reads the file as this
    version does, through the upgrade steps.
    """

    from assistant_gateway.config import migrate_instance_config_file
    from tests.pinned_memory_support import zero_nine_instance

    path = zero_nine_instance(tmp_path, "alpha", "team_a")
    if upgraded_first:
        migrate_instance_config_file(path)

    out = _export(tmp_path, path)

    assert [record.dotted for record in _records(out)] == []
    with TransferReader.open(out, PASSPHRASE) as reader:
        plan = import_team(reader, tmp_path / "new", dry_run=False, sections=(SettingsSection(),), require_valid=True)
    assert plan.records == [] and not plan.by_outcome(RecordOutcome.REFUSED)


@pytest.mark.parametrize("source_version", [12, 13])
def test_version_12_and_13_exports_import_with_ssh_guardrail_defaults(
    tmp_path: Path, source_version: int
) -> None:
    """Version 12 and 13 exports have no execution_control.ssh guardrail fields; version 14 fills defaults."""

    from assistant_gateway.config import GatewayInstanceConfig
    from default_values import DEFAULT_SSH_DENIED_CIDRS

    class ExportedAsOlder(SettingsSection):
        schema_version = source_version

        def export(self, source: ExportSource, files: SectionFiles) -> SectionExport:
            record = SettingV1(path=("execution_control", "ssh", "enabled"), value=True)
            return SectionExport(records=[record.model_dump(mode="json")])

    path, _config = _old_instance(tmp_path)
    target = tmp_path / "new"
    plan = _import(_export(tmp_path, path, ExportedAsOlder()), target, dry_run=False)

    assert not plan.by_outcome(RecordOutcome.REFUSED)
    imported = GatewayInstanceConfig.load(target / "instance_config.yaml")
    assert imported.execution_control.ssh.enabled is True
    assert imported.execution_control.ssh.allowed_cidrs == ()
    assert imported.execution_control.ssh.allowed_domains == ()
    assert imported.execution_control.ssh.denied_cidrs == DEFAULT_SSH_DENIED_CIDRS
    assert imported.execution_control.ssh.max_delegation_ttl_seconds == 7200
    assert imported.execution_control.ssh.allow_password_bootstrap is False


def test_version_15_export_upgrades_with_blob_defaults_and_v16_carries_blob_settings(tmp_path: Path) -> None:
    """Settings v16 adds agent.blob_retention_days and transport.queues.max_bytes.blobs with v15 forward conversion."""

    from assistant_gateway.config import GatewayInstanceConfig

    class ExportedAsV15(SettingsSection):
        schema_version = 15

        def export(self, source: ExportSource, files: SectionFiles) -> SectionExport:
            record = SettingV1(path=("heartbeat", "enabled"), value=False)
            return SectionExport(records=[record.model_dump(mode="json")])

    path, _config = _old_instance(tmp_path)
    target = tmp_path / "from-v15"
    plan = _import(_export(tmp_path, path, ExportedAsV15()), target, dry_run=False)
    assert not plan.by_outcome(RecordOutcome.REFUSED)
    imported_v15 = GatewayInstanceConfig.load(target / "instance_config.yaml")
    assert imported_v15.agent.blob_retention_days == 7
    assert imported_v15.transport.queues.max_bytes.blobs == 512 * MIB

    def customized_blobs(config: Any) -> None:
        config.agent.blob_retention_days = 14
        config.transport.queues.max_bytes.blobs = 256 * MIB

    path2, _config2 = _old_instance(tmp_path / "src-v16", customized_blobs)
    out2 = _export(tmp_path / "src-v16", path2)
    assert {record.dotted: record.value for record in _records(out2)} == {
        "agent.blob_retention_days": 14,
        "transport.queues.max_bytes.blobs": 256 * MIB,
    }
    target2 = tmp_path / "to-v16"
    plan2 = _import(out2, target2, dry_run=False)
    assert not plan2.by_outcome(RecordOutcome.REFUSED)
    imported_v16 = GatewayInstanceConfig.load(target2 / "instance_config.yaml")
    assert imported_v16.agent.blob_retention_days == 14
    assert imported_v16.transport.queues.max_bytes.blobs == 256 * MIB


def test_version_16_export_upgrades_with_wait_limit_defaults_and_v17_carries_the_pair(tmp_path: Path) -> None:
    """Settings v17 carries both timed-wait limits together and upgrades v16 exports with their defaults.

    The round-trip pair (5, 10) has ``with_approval`` below the default ``without_approval`` (20),
    so importing proves both settings are applied together before cross-field validation runs.
    """

    from assistant_gateway.config import GatewayInstanceConfig

    class ExportedAsV16(SettingsSection):
        schema_version = 16

        def export(self, source: ExportSource, files: SectionFiles) -> SectionExport:
            record = SettingV1(path=("heartbeat", "enabled"), value=False)
            return SectionExport(records=[record.model_dump(mode="json")])

    path, _config = _old_instance(tmp_path / "src-v16")
    target = tmp_path / "from-v16"
    plan = _import(_export(tmp_path / "src-v16", path, ExportedAsV16()), target, dry_run=False)
    assert not plan.by_outcome(RecordOutcome.REFUSED)
    imported_v16 = GatewayInstanceConfig.load(target / "instance_config.yaml")
    assert imported_v16.agent.max_wait_minutes_without_approval == 20
    assert imported_v16.agent.max_wait_minutes_with_approval == 1440

    def customized_waits(config: Any) -> None:
        config.agent.max_wait_minutes_without_approval = 5
        config.agent.max_wait_minutes_with_approval = 10

    path2, _config2 = _old_instance(tmp_path / "src-v17", customized_waits)
    out2 = _export(tmp_path / "src-v17", path2)
    assert {record.dotted: record.value for record in _records(out2)} == {
        "agent.max_wait_minutes_without_approval": 5,
        "agent.max_wait_minutes_with_approval": 10,
    }
    target2 = tmp_path / "to-v17"
    plan2 = _import(out2, target2, dry_run=False)
    assert not plan2.by_outcome(RecordOutcome.REFUSED)
    imported_v17 = GatewayInstanceConfig.load(target2 / "instance_config.yaml")
    assert imported_v17.agent.max_wait_minutes_without_approval == 5
    assert imported_v17.agent.max_wait_minutes_with_approval == 10


def test_an_invalid_wait_limit_pair_refuses_carried_agent_settings(tmp_path: Path) -> None:
    """A wait-limit ordering failure blames ``('agent',)`` and refuses carried ``agent.*`` settings together."""

    from assistant_gateway.config import GatewayInstanceConfig

    class CraftedConflict(SettingsSection):
        def export(self, source: ExportSource, files: SectionFiles) -> SectionExport:
            records = [
                SettingV1(path=("heartbeat", "enabled"), value=False),
                SettingV1(path=("agent", "blob_retention_days"), value=14),
                SettingV1(path=("agent", "max_wait_minutes_without_approval"), value=30),
                SettingV1(path=("agent", "max_wait_minutes_with_approval"), value=25),
            ]
            return SectionExport(records=[record.model_dump(mode="json") for record in records])

    path, _config = _old_instance(tmp_path)
    out = _export(tmp_path, path, CraftedConflict())
    target = tmp_path / "new"

    plan = _import(out, target, dry_run=False)

    outcomes = {record.key: record for record in plan.records}
    assert outcomes["heartbeat.enabled"].outcome is RecordOutcome.IMPORT
    for refused_key in (
        "agent.blob_retention_days",
        "agent.max_wait_minutes_without_approval",
        "agent.max_wait_minutes_with_approval",
    ):
        assert outcomes[refused_key].outcome is RecordOutcome.REFUSED, refused_key
        assert "must be at least agent.max_wait_minutes_without_approval (30)" in outcomes[refused_key].reason
    imported = GatewayInstanceConfig.load(target / "instance_config.yaml")
    assert imported.heartbeat.enabled is False
    assert imported.agent.blob_retention_days == 7
    assert imported.agent.max_wait_minutes_without_approval == 20
    assert imported.agent.max_wait_minutes_with_approval == 1440
