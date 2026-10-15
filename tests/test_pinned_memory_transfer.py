"""``assistant_cli memory export|import`` moves pinned memories between two real instances.

Each test builds two instances from saved ``instance_config.yaml`` files with
real SQLite stores, runs the command line the way an operator does, and reads
the stores back through the pinned memory repository.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import pytest

from custom_agents.pinned_memory_transfer import (
    IMPORT_ACTOR,
    IMPORTED_FROM_KEY,
    ImportOutcome,
    PersonTargets,
    PinnedMemoryExportV1,
    plan_import,
)
from custom_messages.contracts.access_v2 import PersonStanding
from storage.pinned_memory_repository import COREMEM_BANK, USERMEM_BANK
from tests.pinned_memory_support import new_instance as _instance
from tests.pinned_memory_support import record_people as _people
from tests.pinned_memory_support import repository as _repo
from tests.pinned_memory_support import row_count as _count
from tests.pinned_memory_support import run_cli as _cli

OLD_TEAM = "team_old"
NEW_TEAM = "team_new"
PAST = "2020-01-01T00:00:00+00:00"
FUTURE = "2099-01-01T00:00:00+00:00"
AUTHOR = {"kind": "user", "id": "usr_old_a", "display_name": "Ana"}
SOURCE = {"conversation_id": "conv_secret_ref", "task_id": "task_1", "chat_id": "chat_42", "channel_name": "telegram"}


@pytest.fixture
def old(tmp_path: Path) -> tuple[Path, Any]:
    path, config = _instance(tmp_path, "old", OLD_TEAM)
    repo = _repo(config)
    repo.create_coremem(
        team_id=OLD_TEAM,
        fact_text="The team ships on Fridays.",
        capacity=10,
        tags=["process"],
        metadata={"topic": "release"},
        source=SOURCE,
        actor=AUTHOR,
    )
    repo.create_coremem(team_id=OLD_TEAM, fact_text="New policy starts later.", capacity=10, valid_from_utc=FUTURE)
    repo.create_usermem(
        canonical_user_id="usr_old_a",
        fact_text="Ana prefers short answers.",
        capacity=10,
        source_key="onboarding:user_context:v1",
        actor=AUTHOR,
    )
    repo.create_usermem(canonical_user_id="usr_old_a", fact_text="Ana lives in Lisbon.", capacity=10)
    repo.create_usermem(canonical_user_id="usr_b", fact_text="Ben is on call on Mondays.", capacity=10)
    repo.create_usermem(canonical_user_id="usr_gone", fact_text="Held until deletion.", capacity=10)
    # Expired but not pruned yet: the export must neither include nor prune it.
    repo.create_usermem(canonical_user_id="usr_b", fact_text="Old expired note.", capacity=10, invalid_after_utc=PAST)
    _people(
        config,
        OLD_TEAM,
        {"usr_old_a": PersonStanding.ACTIVE, "usr_b": PersonStanding.ACTIVE, "usr_gone": PersonStanding.REMOVED},
    )
    return path, config


def _export(old_path: Path, tmp_path: Path, *extra: str) -> Path:
    out = tmp_path / "memories.json"
    _cli("export", "--config", str(old_path), "--out", str(out), *extra)
    return out


def _texts(rows: list[Any]) -> list[str]:
    return sorted(row.fact_text for row in rows)


def test_export_import_round_trip_maps_people_and_keeps_history(old, tmp_path: Path, capsys) -> None:
    old_path, old_config = old
    facts_before, events_before = _count(old_config, "pinned_memory_facts_active"), _count(old_config, "pinned_memory_fact_events")

    out = _export(old_path, tmp_path)

    # The export only reads: nothing pruned, no history written.
    assert _count(old_config, "pinned_memory_facts_active") == facts_before
    assert _count(old_config, "pinned_memory_fact_events") == events_before
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    raw = out.read_text(encoding="utf-8")
    document = PinnedMemoryExportV1.model_validate_json(raw)
    assert document.schema_version == 1 and document.source_instance == "old" and document.source_team_id == OLD_TEAM
    assert "Held until deletion." not in raw and "Old expired note." not in raw
    assert "conv_secret_ref" not in raw and "chat_42" not in raw
    printed = capsys.readouterr().out
    assert "Wrote 5 pinned memories" in printed and "usr_gone" in printed

    new_path, new_config = _instance(tmp_path, "new", NEW_TEAM)
    _people(new_config, NEW_TEAM, {"usr_new_a": PersonStanding.ACTIVE, "usr_b": PersonStanding.ACTIVE})
    _cli("import", "--config", str(new_path), "--in", str(out), "--map-person", "usr_old_a=usr_new_a")

    printed = capsys.readouterr().out
    assert "Imported 5 of 5" in printed and "Person usr_new_a (was usr_old_a)" in printed
    _assert_arrived_with_history(_repo(new_config))


def _assert_arrived_with_history(new_repo: Any) -> None:
    """Facts sit under the new owners, keep their content, and carry the import's history."""

    team = new_repo.list_unexpired_facts()
    assert _texts([row for row in team if row.bank == COREMEM_BANK and row.team_id == NEW_TEAM]) == [
        "New policy starts later.",
        "The team ships on Fridays.",
    ]
    ana = new_repo.list_usermem(canonical_user_id="usr_new_a")
    assert _texts(ana) == ["Ana lives in Lisbon.", "Ana prefers short answers."]
    assert _texts(new_repo.list_usermem(canonical_user_id="usr_b")) == ["Ben is on call on Mondays."]
    assert new_repo.list_usermem(canonical_user_id="usr_old_a") == []

    shipped = next(row for row in team if row.fact_text == "The team ships on Fridays.")
    assert shipped.tags == ["process"] and shipped.metadata["topic"] == "release"
    assert (shipped.created_by_kind, shipped.created_by_id) == (IMPORT_ACTOR["kind"], IMPORT_ACTOR["id"])
    origin = shipped.metadata[IMPORTED_FROM_KEY]
    assert origin["instance"] == "old" and origin["created_by_id"] == "usr_old_a" and origin["created_at_utc"]
    later = next(row for row in team if row.fact_text == "New policy starts later.")
    assert later.valid_from_utc == FUTURE
    preference = next(row for row in ana if row.fact_text == "Ana prefers short answers.")
    assert preference.source_key == "onboarding:user_context:v1"
    event = new_repo.get_latest_event(bank=USERMEM_BANK, canonical_user_id="usr_new_a", memory_id=preference.memory_id)
    assert event is not None and event.event_type == "create" and event.actor_id == IMPORT_ACTOR["id"]


def test_running_the_import_again_skips_facts_already_there(old, tmp_path: Path, capsys) -> None:
    out = _export(old[0], tmp_path)
    new_path, new_config = _instance(tmp_path, "new", NEW_TEAM)
    _people(new_config, NEW_TEAM, {"usr_old_a": PersonStanding.ACTIVE, "usr_b": PersonStanding.ACTIVE})
    _cli("import", "--config", str(new_path), "--in", str(out))
    facts, events = _count(new_config, "pinned_memory_facts_active"), _count(new_config, "pinned_memory_fact_events")
    capsys.readouterr()

    _cli("import", "--config", str(new_path), "--in", str(out))

    printed = capsys.readouterr().out
    assert "Imported 0 of 5" in printed and "already there" in printed
    assert _count(new_config, "pinned_memory_facts_active") == facts
    assert _count(new_config, "pinned_memory_fact_events") == events


def test_people_with_no_known_target_are_refused_and_listed(old, tmp_path: Path, capsys) -> None:
    out = _export(old[0], tmp_path)
    new_path, new_config = _instance(tmp_path, "new", NEW_TEAM)
    _people(new_config, NEW_TEAM, {"usr_b": PersonStanding.ACTIVE, "usr_x": PersonStanding.REMOVED})
    capsys.readouterr()

    with pytest.raises(SystemExit) as exit_info:
        _cli("import", "--config", str(new_path), "--in", str(out), "--map-person", "usr_b=usr_x")

    assert exit_info.value.code == 1
    printed = capsys.readouterr().out
    assert "Not imported (3):" in printed
    assert "usr_old_a is not a person in the new instance's team" in printed
    assert "usr_x was removed from the new instance's team" in printed
    repo = _repo(new_config)
    assert repo.list_usermem(canonical_user_id="usr_old_a") == [] and repo.list_usermem(canonical_user_id="usr_x") == []
    # The team's memory still arrived.
    assert len([row for row in repo.list_unexpired_facts() if row.bank == COREMEM_BANK]) == 2


def test_without_people_records_only_mapped_people_are_imported(old, tmp_path: Path, capsys) -> None:
    out = _export(old[0], tmp_path, "--person", "usr_old_a", "--person", "usr_b")
    new_path, new_config = _instance(tmp_path, "new", NEW_TEAM)
    capsys.readouterr()

    with pytest.raises(SystemExit):
        _cli("import", "--config", str(new_path), "--in", str(out), "--map-person", "usr_old_a=usr_new_a")

    printed = capsys.readouterr().out
    assert "start its gateway once, or map the person" in printed
    assert "mapped person ids were not checked" in printed
    repo = _repo(new_config)
    assert len(repo.list_usermem(canonical_user_id="usr_new_a")) == 2
    assert repo.list_usermem(canonical_user_id="usr_b") == []
    # --person without --team exports no team memory.
    assert [row for row in repo.list_unexpired_facts() if row.bank == COREMEM_BANK] == []


def test_facts_that_do_not_fit_or_break_limits_are_reported(old, tmp_path: Path, capsys) -> None:
    out = _export(old[0], tmp_path, "--team")
    new_path, new_config = _instance(tmp_path, "new", NEW_TEAM, memory={"coremem": {"max_facts_per_team": 2, "max_tags": 0}})
    _repo(new_config).create_coremem(team_id=NEW_TEAM, fact_text="Already here.", capacity=2)
    capsys.readouterr()

    with pytest.raises(SystemExit):
        _cli("import", "--config", str(new_path), "--in", str(out))

    printed = capsys.readouterr().out
    # The tagged fact breaks max_tags=0; the other takes the last place.
    assert "refused: breaks the new instance's limits: too many tags" in printed
    assert "Imported 1 of 2" in printed
    extra = tmp_path / "more.json"
    document = PinnedMemoryExportV1.model_validate_json(out.read_text(encoding="utf-8"))
    document.facts[0] = document.facts[0].model_copy(update={"fact_text": "A third fact.", "tags": []})
    extra.write_text(document.model_dump_json(), encoding="utf-8")
    capsys.readouterr()

    with pytest.raises(SystemExit):
        _cli("import", "--config", str(new_path), "--in", str(extra))

    assert "no room: the coremem bank is full (2 facts)" in capsys.readouterr().out
    assert len(_repo(new_config).list_unexpired_facts()) == 2


def test_dry_run_changes_nothing(old, tmp_path: Path, capsys) -> None:
    out = _export(old[0], tmp_path)
    new_path, new_config = _instance(tmp_path, "new", NEW_TEAM)
    new_db = Path(new_config.manager_storage_settings.sqlite_path)
    capsys.readouterr()

    with pytest.raises(SystemExit):
        _cli("import", "--config", str(new_path), "--in", str(out), "--dry-run", "--map-person", "usr_old_a=usr_a2")

    assert not new_db.exists()  # a dry run does not even create the database
    printed = capsys.readouterr().out
    assert "Would import 4 of 5" in printed and "Dry run: nothing was changed." in printed

    _people(new_config, NEW_TEAM, {"usr_old_a": PersonStanding.ACTIVE, "usr_b": PersonStanding.ACTIVE})
    _repo(new_config)  # the new instance has started once: its tables exist, empty
    facts, events = _count(new_config, "pinned_memory_facts_active"), _count(new_config, "pinned_memory_fact_events")
    _cli("import", "--config", str(new_path), "--in", str(out), "--dry-run")

    assert "Would import 5 of 5" in capsys.readouterr().out
    assert _count(new_config, "pinned_memory_facts_active") == facts == 0
    assert _count(new_config, "pinned_memory_fact_events") == events == 0


def test_team_id_option_chooses_the_receiving_team(old, tmp_path: Path, capsys) -> None:
    out = _export(old[0], tmp_path, "--team")
    new_path, new_config = _instance(tmp_path, "new", NEW_TEAM)

    _cli("import", "--config", str(new_path), "--in", str(out), "--team-id", "team_other")

    rows = _repo(new_config).list_unexpired_facts()
    assert {row.team_id for row in rows} == {"team_other"}
    assert "Team memory (coremem, team team_other): 2 imported" in capsys.readouterr().out


def test_the_planner_skips_repeats_within_the_file_and_expired_facts() -> None:
    fact = {
        "bank": USERMEM_BANK,
        "person_id": "usr_a",
        "memory_id": 1,
        "fact_text": "Same text.",
        "valid_from_utc": PAST,
        "created_by_kind": "user",
        "created_at_utc": PAST,
        "updated_at_utc": PAST,
    }
    document = PinnedMemoryExportV1.model_validate(
        {
            "exported_at_utc": PAST,
            "source_instance": "old",
            "source_team_id": OLD_TEAM,
            "facts": [fact, {**fact, "memory_id": 2}, {**fact, "memory_id": 3, "fact_text": "Gone.", "invalid_after_utc": PAST}],
        }
    )

    plan = plan_import(
        document,
        existing_rows=[],
        team_id=NEW_TEAM,
        targets=PersonTargets(explicit={}, known={"usr_a": PersonStanding.DISABLED}),
        memory_config={},
    )

    assert [item.outcome for item in plan] == [ImportOutcome.IMPORTED, ImportOutcome.ALREADY_THERE, ImportOutcome.EXPIRED]


def test_the_import_refuses_a_file_it_cannot_read(tmp_path: Path) -> None:
    new_path, _ = _instance(tmp_path, "new", NEW_TEAM)
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"format": "something-else", "schema_version": 9}), encoding="utf-8")

    with pytest.raises(SystemExit, match="is not a memory export this version can read"):
        _cli("import", "--config", str(new_path), "--in", str(bad))


def test_the_export_never_overwrites_a_file(old, tmp_path: Path) -> None:
    out = tmp_path / "memories.json"
    out.write_text("keep me", encoding="utf-8")

    with pytest.raises(SystemExit, match="already exists"):
        _cli("export", "--config", str(old[0]), "--out", str(out))

    assert out.read_text(encoding="utf-8") == "keep me"
