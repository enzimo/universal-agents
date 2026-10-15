"""The ``pinned_memory`` transfer section carries team and personal pinned facts under the same person ids."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from storage.pinned_memory_repository import COREMEM_BANK, USERMEM_BANK, create_pinned_memory_repository
from team_transfer.container import TransferReader
from team_transfer.crypto import new_key_derivation
from team_transfer.people import PeopleSection
from team_transfer.pinned_memory import PinnedMemorySection
from team_transfer.sections import RecordOutcome
from team_transfer.service import export_source, export_team, import_team
from team_transfer.settings import SettingsSection
from tests.pinned_memory_support import new_instance

PASSPHRASE = "correct horse battery staple"
FAST_KEYS = {"opslimit": 1, "memlimit": 8192}
SECTIONS = (SettingsSection(), PeopleSection(), PinnedMemorySection())
TEAM = "team_a"
AUTHOR = {"kind": "user", "id": "usr_ana", "display_name": "Ana"}


def _old_instance(tmp_path: Path) -> Path:
    from storage.gateway_state_repository import create_gateway_state_repository

    path, config = new_instance(tmp_path, "alpha", TEAM)
    people = create_gateway_state_repository(config.state_storage_settings)
    for person, role, status in (("usr_ana", "owner", "active"), ("usr_ben", "member", "active"), ("usr_gone", "member", "removed")):
        people.create_gateway_user(canonical_user_id=person, display_name=person, timezone="UTC")
        people.create_team_membership(canonical_user_id=person, team_id=TEAM, role=role, status=status)
    memory = create_pinned_memory_repository(config.manager_storage_settings)
    memory.create_coremem(team_id=TEAM, fact_text="The team ships on Fridays.", capacity=10, tags=["process"], actor=AUTHOR)
    memory.create_usermem(canonical_user_id="usr_ana", fact_text="Ana prefers short answers.", capacity=10, actor=AUTHOR)
    memory.create_usermem(canonical_user_id="usr_ben", fact_text="Ben is on call on Mondays.", capacity=10)
    memory.create_usermem(canonical_user_id="usr_gone", fact_text="Held until deletion.", capacity=10)
    memory.create_usermem(canonical_user_id="usr_stranger", fact_text="Not in the team.", capacity=10)
    return path


def _transfer(tmp_path: Path, config_path: Path, *, dry_run: bool = False) -> tuple[Any, Path]:
    out = tmp_path / "team.uatransfer"
    export_team(
        export_source(config_path), out, PASSPHRASE, owner_person_id="usr_ana", sections=SECTIONS,
        key_derivation=new_key_derivation(**FAST_KEYS),
    )
    target = tmp_path / "new"
    with TransferReader.open(out, PASSPHRASE) as reader:
        plan = import_team(reader, target, dry_run=dry_run, sections=SECTIONS)
    return plan, target


def test_pinned_facts_arrive_under_the_same_people(tmp_path: Path) -> None:
    from assistant_gateway.config import GatewayInstanceConfig

    plan, target = _transfer(tmp_path, _old_instance(tmp_path))

    outcomes = {r.key.rsplit("/", 1)[0]: r for r in plan.records if r.section == "pinned_memory"}
    assert {key for key, r in outcomes.items() if r.outcome is RecordOutcome.IMPORT} == {
        "coremem/team",
        "usermem/usr_ana",
        "usermem/usr_ben",
    }
    assert "usermem/usr_gone" not in outcomes, "a removed person's facts stay behind"
    assert outcomes["usermem/usr_stranger"].outcome is RecordOutcome.REFUSED
    assert "usr_stranger, who is not imported" in outcomes["usermem/usr_stranger"].reason

    config = GatewayInstanceConfig.load(target / "instance_config.yaml")
    facts = create_pinned_memory_repository(config.manager_storage_settings).list_unexpired_facts()
    arrived = {(fact.bank, fact.team_id if fact.bank == COREMEM_BANK else fact.canonical_user_id, fact.fact_text) for fact in facts}
    assert arrived == {
        (COREMEM_BANK, TEAM, "The team ships on Fridays."),
        (USERMEM_BANK, "usr_ana", "Ana prefers short answers."),
        (USERMEM_BANK, "usr_ben", "Ben is on call on Mondays."),
    }
    team_fact = next(fact for fact in facts if fact.bank == COREMEM_BANK)
    assert team_fact.tags == ["process"]
    assert team_fact.metadata["imported_from"]["created_by_id"] == "usr_ana"


def test_a_dry_run_plans_the_same_and_writes_nothing(tmp_path: Path) -> None:
    plan, target = _transfer(tmp_path, _old_instance(tmp_path), dry_run=True)

    assert sum(1 for r in plan.records if r.section == "pinned_memory" and r.outcome is RecordOutcome.IMPORT) == 3
    assert not (target / "instance_config.yaml").exists()
