"""Failures reproduced during the export/import review, through the real transfer path."""

from datetime import datetime, timedelta, timezone

import pytest
import yaml

from assistant_gateway.config import GatewayInstanceConfig
from internal_credentials import parse_env_file
from runtime_settings.instance_layout import InstanceLayout
from storage.access_repository import AccessRepository
from storage.gateway_state_repository import create_gateway_state_repository
from team_transfer.access import AccessSection
from team_transfer.container import TransferReader
from team_transfer.control import ControlSection
from team_transfer.crypto import new_key_derivation
from team_transfer.operator_secrets import OperatorSecretsSection
from team_transfer.people import PeopleSection
from team_transfer.sections import RecordOutcome, SectionExport
from team_transfer.service import TransferError, export_source, export_team, import_team
from team_transfer.settings import SettingsSection
from tests.pinned_memory_support import new_instance

PASSPHRASE = "correct horse battery staple"
TEAM = "team_a"


def transfer(tmp_path, path, sections):
    out = tmp_path / "team.uatransfer"
    export_team(
        export_source(path),
        out,
        PASSPHRASE,
        owner_person_id="usr_owner",
        sections=sections,
        key_derivation=new_key_derivation(opslimit=1, memlimit=8192),
    )
    target = tmp_path / "new"
    with TransferReader.open(out, PASSPHRASE) as reader:
        plan = import_team(reader, target, dry_run=False, sections=sections)
    return plan, GatewayInstanceConfig.load(target / "instance_config.yaml")


def owner(config):
    repository = create_gateway_state_repository(config.state_storage_settings)
    repository.create_gateway_user(
        canonical_user_id="usr_owner", display_name="Owner", timezone="UTC"
    )
    repository.create_team_membership(
        canonical_user_id="usr_owner", team_id=TEAM, role="owner"
    )
    return repository


def test_disabled_membership_does_not_reactivate_its_active_global_user(tmp_path):
    path, config = new_instance(tmp_path, "alpha", TEAM)
    repository = owner(config)
    repository.create_gateway_user(
        canonical_user_id="usr_disabled", display_name="Disabled", timezone="UTC"
    )
    repository.create_team_membership(
        canonical_user_id="usr_disabled", team_id=TEAM, role="member", status="disabled"
    )
    repository.create_web_user_token(
        token_hash="d" * 64, canonical_user_id="usr_disabled", label="old"
    )
    _plan, imported = transfer(tmp_path, path, (SettingsSection(), PeopleSection()))
    actual = create_gateway_state_repository(imported.state_storage_settings)
    assert actual.get_gateway_user("usr_disabled").status == "disabled"
    assert actual.resolve_web_user_token(token_hash="d" * 64) is None


def test_external_nats_and_configured_secret_names_move_and_paths_rebase(tmp_path):
    path, config = new_instance(tmp_path, "alpha", TEAM)
    config.transport.nats_url = "nats://external.example:4222"
    config.transport.nats_servers = [config.transport.nats_url]
    config.interactive_apps.photon.project_secret_env_var = "CUSTOM_PHOTON_SECRET"
    config.save(path)
    config.layout.team_env_path.write_text(
        "NATS_USER=operator-user\nNATS_PASSWORD=synthetic-password\nCUSTOM_PHOTON_SECRET=synthetic-secret\n"
        f"SECURE_TOOL_BROKER_CREDENTIALS_DIR={config.broker_credentials_path}\n"
    )
    plan, imported = transfer(
        tmp_path, path, (SettingsSection(), OperatorSecretsSection())
    )
    assert not plan.by_outcome(RecordOutcome.REFUSED)
    actual = parse_env_file(imported.layout.team_env_path)
    assert (
        actual["NATS_USER"] == "operator-user"
        and actual["NATS_PASSWORD"] == "synthetic-password"
    )
    assert actual["CUSTOM_PHOTON_SECRET"] == "synthetic-secret"
    assert actual["SECURE_TOOL_BROKER_CREDENTIALS_DIR"] == str(
        imported.broker_credentials_path
    )
    assert str(config.layout.root) not in actual["SECURE_TOOL_BROKER_CREDENTIALS_DIR"]


def test_a_replaced_parent_is_written_before_its_older_child(tmp_path):
    from custom_messages.contracts.access_v2 import (
        AccessLevel,
        GrantV1,
        SubjectKind,
        SubjectRefV1,
        TargetKind,
        TargetRefV1,
    )

    path, config = new_instance(tmp_path, "alpha", TEAM)
    owner(config)
    store = AccessRepository(config.manager_storage_settings)
    now = datetime.now(timezone.utc)
    person = SubjectRefV1(kind=SubjectKind.USER, id="usr_owner")
    parent = GrantV1(
        grant_id="old_parent",
        team_id=TEAM,
        subject=person,
        target=TargetRefV1(kind=TargetKind.TOOL, id="fn:schedules"),
        level=AccessLevel.READ,
        delegation_depth=1,
        granted_by=person,
        created_at_utc=now,
    )
    child = parent.model_copy(
        update={
            "grant_id": "child",
            "parent_grant_id": parent.grant_id,
            "subject": SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{TEAM}/TeamArchitect"),
            "for_person_id": "usr_owner",
            "created_at_utc": now + timedelta(seconds=1),
        }
    )
    replacement = parent.model_copy(
        update={"grant_id": "new_parent", "created_at_utc": now + timedelta(seconds=2)}
    )
    store.create(parent)
    store.create(child)
    store.create(replacement, replaces=[parent.grant_id])
    plan, imported = transfer(
        tmp_path, path, (SettingsSection(), PeopleSection(), AccessSection())
    )
    assert not plan.by_outcome(RecordOutcome.REFUSED)
    actual = AccessRepository(imported.manager_storage_settings)
    assert actual.get("child").parent_grant_id == "new_parent"
    assert {grant.grant_id for grant in actual.list_team(TEAM)} == {
        "child",
        "new_parent",
    }


@pytest.mark.parametrize("dsn_source", ["env_file", "environment", "secret_file"])
def test_postgres_writers_and_next_launch_use_the_planned_new_database(
    tmp_path, monkeypatch, dsn_source
):
    path, config = new_instance(tmp_path, "alpha", TEAM)
    saved = config.to_yaml_dict()
    for section in ("state", "gateway"):
        saved[section]["storage"]["backend"] = "postgres"
    path.write_text(yaml.safe_dump(saved))
    target = tmp_path / "new"
    target.mkdir()
    dsn = "postgresql://synthetic@localhost/new_empty"
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    monkeypatch.delenv("POSTGRES_DSN_FILE", raising=False)
    if dsn_source == "environment":
        monkeypatch.setenv("POSTGRES_DSN", dsn)
    elif dsn_source == "env_file":
        (target / "team.env").write_text(f"POSTGRES_DSN={dsn}\n")
    else:
        secret = tmp_path / "dsn"
        secret.write_text(dsn)
        (target / "team.env").write_text(f"POSTGRES_DSN_FILE={secret}\n")
    seen = []
    from contextlib import contextmanager
    from types import SimpleNamespace

    @contextmanager
    def empty_database(actual_dsn):
        from storage.read_only import repositories_are_read_only

        assert actual_dsn == dsn and repositories_are_read_only()
        yield SimpleNamespace(
            execute=lambda query: SimpleNamespace(fetchone=lambda: (False,))
        )

    monkeypatch.setattr("storage.postgres_connection.connect_postgres", empty_database)

    class StorageConsumer:
        name = "storage_consumer"
        schema_version = 1
        encrypted = after_start = False

        def export(self, source, files):
            return SectionExport()

        def plan(self, records, context):
            return []

        def apply(self, records, plan, context):
            actual = GatewayInstanceConfig.load_for_process(
                context.layout.instance_config_path
            )
            seen.extend(
                [
                    actual.manager_storage_settings.postgres_dsn,
                    actual.state_storage_settings.postgres_dsn,
                ]
            )

    _plan, imported = transfer(tmp_path, path, (SettingsSection(), StorageConsumer()))
    assert seen == [dsn, dsn]
    saved_env = parse_env_file(imported.layout.team_env_path)
    from env_secrets import get_env_with_file_fallback

    assert get_env_with_file_fallback("POSTGRES_DSN", env=saved_env) == dsn
    assert imported.resolve_manager_storage_settings(env=saved_env).postgres_dsn == dsn
    assert imported.resolve_state_storage_settings(env=saved_env).postgres_dsn == dsn


def test_untouched_model_overrides_use_new_release_defaults(tmp_path, monkeypatch):
    from assistant_gateway.bootstrap import bootstrap_instance
    from config_loader import load_persistent_llm_override

    path, config = new_instance(tmp_path, "alpha", TEAM)
    bootstrap_instance(config)
    original_create = GatewayInstanceConfig.create
    out = tmp_path / "team.uatransfer"
    sections = (SettingsSection(), ControlSection())
    export_team(
        export_source(path),
        out,
        PASSPHRASE,
        owner_person_id="usr_owner",
        sections=sections,
        key_derivation=new_key_derivation(opslimit=1, memlimit=8192),
    )

    def new_defaults(**kwargs):
        config = original_create(**kwargs)
        config.llm.model_id = "new-release-model"
        return config

    monkeypatch.setattr(GatewayInstanceConfig, "create", new_defaults)
    target = tmp_path / "new"
    with TransferReader.open(out, PASSPHRASE) as reader:
        import_team(reader, target, dry_run=False, sections=sections)
    imported = GatewayInstanceConfig.load(target / "instance_config.yaml")
    override = load_persistent_llm_override(
        instance_root=str(target), agent_type=imported.gateway.named_agent_type
    )
    assert imported.llm.model_id == override["model_id"] == "new-release-model"


def test_roster_references_are_checked_after_invalid_definitions_are_refused(tmp_path):
    from assistant_gateway.bootstrap import bootstrap_instance
    from tests.test_team_transfer_control import OLD_FIELD_DEFINITION

    path, config = new_instance(tmp_path, "alpha", TEAM)
    bootstrap_instance(config)
    definitions = config.layout.control_dir / "agent_definitions"
    definitions.mkdir(exist_ok=True)
    (definitions / "old.yaml").write_text(OLD_FIELD_DEFINITION)
    roster_path = config.layout.control_dir / "TEAM_AGENTS.yaml"
    roster = yaml.safe_load(roster_path.read_text())
    roster["startup_agents"]["agents"] = [
        {"named_agent_type": "OldStyle", "count": 1, "enabled": True}
    ]
    roster_path.write_text(yaml.safe_dump(roster))
    plan, imported = transfer(tmp_path, path, (SettingsSection(), ControlSection()))
    outcomes = {record.key: record for record in plan.records}
    assert outcomes["TEAM_AGENTS.yaml"].outcome is RecordOutcome.REFUSED
    assert "OldStyle" in outcomes["TEAM_AGENTS.yaml"].reason
    assert (
        yaml.safe_load(
            imported.layout.control_dir.joinpath("TEAM_AGENTS.yaml").read_text()
        )["startup_agents"]["agents"]
        == []
    )


def test_every_process_loader_refuses_an_unfinished_import(tmp_path):
    path, config = new_instance(tmp_path, "alpha", TEAM)
    config.layout.team_import_marker_path.parent.mkdir(parents=True)
    config.layout.team_import_marker_path.write_text("{}")
    with pytest.raises(TransferError, match="must not start"):
        GatewayInstanceConfig.load_for_process(path)


def test_nested_onboarding_settings_are_explicitly_reported_as_skipped():
    from assistant_gateway.onboarding_web.settings_import import read_onboarding_import

    raw = "interactive_apps:\n  plugins:\n    builtin.telegram:\n      settings:\n        enabled: true\n        drop_pending_updates: true\n"
    result = read_onboarding_import("instance_config.yaml", raw)
    assert "drop_pending_updates" in str(result)


def test_first_import_refuses_an_occupied_postgres_database(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace
    from team_transfer.settings import require_empty_postgres
    from storage.read_only import repositories_are_read_only

    @contextmanager
    def occupied_database(dsn):
        assert dsn == "postgresql://synthetic@localhost/new_db" and repositories_are_read_only()
        yield SimpleNamespace(
            execute=lambda query: SimpleNamespace(fetchone=lambda: (True,))
        )

    monkeypatch.setattr(
        "storage.postgres_connection.connect_postgres", occupied_database
    )
    config = GatewayInstanceConfig.create(
        instance_name="new", instance_root=str(tmp_path), team_id=TEAM, timezone="UTC"
    )
    config.state.storage.backend = "postgres"
    context = SimpleNamespace(existing=False, scratch={
        "settings.postgres_dsn": "postgresql://synthetic@localhost/new_db",
        "settings.config": config,
    })
    with pytest.raises(TransferError, match="already holds tables"):
        require_empty_postgres(context)
    context.existing = True
    require_empty_postgres(context)


@pytest.mark.parametrize("owner_kind", ["person", "team"])
def test_later_account_link_import_keeps_the_destinations_current_account(tmp_path, owner_kind):
    from custom_messages.contracts.access_v2 import (
        AccountLinkV1, LinkOwnerKind, SubjectKind, SubjectRefV1,
    )

    path, config = new_instance(tmp_path, "alpha", TEAM)
    owner(config)
    link = AccountLinkV1(
        link_id="link_old", team_id=TEAM, tool_id="cli:github",
        owner_kind=LinkOwnerKind(owner_kind),
        owner_id="usr_owner" if owner_kind == "person" else TEAM,
        credential_key="old_credential",
        created_by=SubjectRefV1(kind=SubjectKind.USER, id="usr_owner"),
        created_at_utc=datetime.now(timezone.utc),
    )
    AccessRepository(config.manager_storage_settings).create_link(link)
    sections = (SettingsSection(), PeopleSection(), AccessSection())
    archive, target = tmp_path / "team.uatransfer", tmp_path / "new"
    export_team(export_source(path), archive, PASSPHRASE, owner_person_id="usr_owner",
                sections=sections, key_derivation=new_key_derivation(opslimit=1, memlimit=8192))
    with TransferReader.open(archive, PASSPHRASE) as reader:
        import_team(reader, target, dry_run=False, sections=sections,
                    choices={"people": {"usr_owner"}}, require_valid=True)
        imported = GatewayInstanceConfig.load_saved(target / "instance_config.yaml")
        store = AccessRepository(imported.manager_storage_settings)
        store.create_link(link.model_copy(update={
            "link_id": "link_new", "credential_key": "new_credential",
        }))
        plan = import_team(reader, target, dry_run=False, sections=sections,
                           choices={"access": {"account_link/link_old"}},
                           allow_existing=True, require_valid=True)
        current = store.active_link(TEAM, link.tool_id, link.owner_kind, link.owner_id)
        assert (current.link_id, current.credential_key) == ("link_new", "new_credential")
        assert store.get_link("link_old") is None
        assert any(row.key == "account_link/link_old" and "kept" in row.reason
                   for row in plan.skipped_records)


@pytest.mark.parametrize("membership", ["active", "removed", "absent"])
def test_later_people_import_refuses_a_destination_chat_binding_before_writes(tmp_path, membership):
    path, config = new_instance(tmp_path, "alpha", TEAM)
    source = owner(config)
    source.create_gateway_user(canonical_user_id="usr_member", display_name="Member", timezone="UTC")
    source.create_team_membership(canonical_user_id="usr_member", team_id=TEAM, role="member")
    source.create_channel_binding(canonical_user_id="usr_member", channel_name="telegram", external_user_id="tg-42")
    sections = (SettingsSection(), PeopleSection())
    archive, target = tmp_path / "team.uatransfer", tmp_path / "new"
    export_team(export_source(path), archive, PASSPHRASE, owner_person_id="usr_owner",
                sections=sections, key_derivation=new_key_derivation(opslimit=1, memlimit=8192))
    with TransferReader.open(archive, PASSPHRASE) as reader:
        import_team(reader, target, dry_run=False, sections=sections,
                    choices={"people": {"usr_owner"}}, require_valid=True)
        imported = GatewayInstanceConfig.load_saved(target / "instance_config.yaml")
        destination = create_gateway_state_repository(imported.state_storage_settings)
        destination.create_gateway_user(canonical_user_id="usr_new", display_name="New", timezone="UTC")
        if membership != "absent":
            destination.create_team_membership(canonical_user_id="usr_new", team_id=TEAM, role="member")
        if membership == "removed":
            destination.remove_team_membership(canonical_user_id="usr_new", team_id=TEAM)
        destination.create_channel_binding(canonical_user_id="usr_new", channel_name="telegram", external_user_id="tg-42")
        receipt = imported.layout.team_import_receipt_path.read_bytes()
        arguments = dict(sections=sections, choices={"people": {"usr_member"}},
                         allow_existing=True, require_valid=True)
        plan = import_team(reader, target, dry_run=True, **arguments)
        refused = plan.by_outcome(RecordOutcome.REFUSED)
        assert len(refused) == 1 and "linked to usr_new" in refused[0].reason
        with pytest.raises(TransferError, match="Selected records were refused"):
            import_team(reader, target, dry_run=False, **arguments)
        assert destination.get_gateway_user("usr_member") is None
        assert imported.layout.team_import_receipt_path.read_bytes() == receipt
        assert not imported.layout.team_import_marker_path.exists()
        GatewayInstanceConfig.load_for_process(target / "instance_config.yaml")


@pytest.mark.parametrize("activity_postgres", [False, True])
def test_first_import_checks_effective_postgres_targets_before_any_writes(tmp_path, monkeypatch, activity_postgres):
    from contextlib import contextmanager
    from types import SimpleNamespace
    from storage.read_only import repositories_are_read_only

    path, config = new_instance(tmp_path, "alpha", TEAM)
    saved = config.to_yaml_dict()
    for section in ("state", "gateway"):
        saved[section]["storage"]["backend"] = "postgres"
    if activity_postgres:
        saved["activity_collector"]["storage"]["backend"] = "postgres"
    path.write_text(yaml.safe_dump(saved))
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://synthetic@raw.example:5432/new_db")
    monkeypatch.setenv("TEAM_BUNDLED_POSTGRES_ADDRESS", "bundle.example:5432")
    monkeypatch.delenv("POSTGRES_DSN_FILE", raising=False)
    checked = []
    target = tmp_path / "new"

    @contextmanager
    def destination_database(dsn):
        assert repositories_are_read_only()
        checked.append(dsn)
        occupied = "raw.example" in dsn if activity_postgres else "bundle.example" in dsn
        yield SimpleNamespace(execute=lambda query: SimpleNamespace(fetchone=lambda: (occupied,)))

    monkeypatch.setattr("storage.postgres_connection.connect_postgres", destination_database)
    archive = tmp_path / "team.uatransfer"
    sections = (SettingsSection(),)
    export_team(export_source(path), archive, PASSPHRASE, owner_person_id="usr_owner",
                sections=sections, key_derivation=new_key_derivation(opslimit=1, memlimit=8192))
    with TransferReader.open(archive, PASSPHRASE) as reader:
        with pytest.raises(TransferError, match="already holds tables"):
            import_team(reader, target, dry_run=False, sections=sections, require_valid=True)
    expected = ["postgresql://synthetic@bundle.example:5432/new_db"]
    if activity_postgres:
        expected.append("postgresql://synthetic@raw.example:5432/new_db")
    assert checked == expected
    assert not (target / "instance_config.yaml").exists()
    assert not InstanceLayout.at(target).team_import_marker_path.exists()


def test_invalid_operator_secret_errors_do_not_reveal_values():
    from types import SimpleNamespace
    from team_transfer.service import _plan_records

    section = OperatorSecretsSection()
    context = SimpleNamespace(scratch={})
    with pytest.raises(TransferError) as error:
        _plan_records(
            section,
            [
                {
                    "kind": "environment",
                    "name": "TOKEN",
                    "value": {"secret": "do-not-reveal"},
                }
            ],
            context,
        )
    assert "do-not-reveal" not in str(error.value)
