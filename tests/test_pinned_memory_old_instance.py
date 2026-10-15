"""``memory export`` reads an old instance that this version refuses to load.

The owner runs the export from the new image against an instance that runs an
earlier version (``main`` at ``MAIN_ERA_COMMIT``): its configuration holds
removed settings, its database sits in the old ``workspace/dbs/`` folder, and in
Docker its files are mounted read-only somewhere other than the ``/data`` its
configuration records. These tests build such instances and check that the
export reads them without changing a byte, and that the file imports into a new
instance.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import stat
import subprocess
import sys
import textwrap
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

import pytest
import yaml

from custom_agents.pinned_memory_source import SourceOptions, resolve_export_source
from custom_agents.pinned_memory_transfer import PinnedMemoryExportV1
from custom_messages.contracts.access_v2 import PersonStanding
from storage.pinned_memory_repository import COREMEM_BANK, SqlitePinnedMemoryRepository
from tests.pinned_memory_support import new_instance, record_people, repository, run_cli

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# instance_config.yaml as main's onboarding wrote it for /data (see its header).
MAIN_ERA_CONFIG = PROJECT_ROOT / "tests" / "fixtures" / "pinned_memory" / "main_instance_config.yaml"
# The main branch when the instance layout and access settings changed; the
# owner's old instance runs this version.
MAIN_ERA_COMMIT = "300bd334c7797ed3cc12360d11d0ed01bcc15acd"
OLD_TEAM = "team_old"
NEW_TEAM = "team_new"
AUTHOR = {"kind": "user", "id": "usr_ana", "display_name": "Ana"}

# Runs with main's code on PYTHONPATH: onboards an instance and writes its
# pinned memories through main's own repository.
MAIN_WRITER = textwrap.dedent(
    """
    import json, sys
    from assistant_gateway.config import GatewayInstanceConfig
    from storage.pinned_memory_repository import create_pinned_memory_repository

    root = sys.argv[1]
    config = GatewayInstanceConfig.create(
        instance_name="old-team", instance_root=root, team_id="team_old", timezone="UTC"
    )
    config.save(root + "/instance_config.yaml")
    repo = create_pinned_memory_repository(config.manager_storage_settings)
    author = {"kind": "user", "id": "usr_ana", "display_name": "Ana"}
    repo.create_coremem(team_id="team_old", fact_text="The team ships on Fridays.", capacity=10,
                        tags=["process"], source={"conversation_id": "conv_1"}, actor=author)
    repo.create_usermem(canonical_user_id="usr_ana", fact_text="Ana prefers short answers.", capacity=10,
                        source_key="onboarding:user_context:v1", actor=author)
    repo.create_usermem(canonical_user_id="usr_ben", fact_text="Ben is on call on Mondays.", capacity=10)
    print(json.dumps({"database": config.manager_storage_settings.sqlite_path}))
    """
)


def _write_old_memories(database: Path) -> None:
    """The same facts MAIN_WRITER writes; the table is unchanged since 0.1.0."""

    database.parent.mkdir(parents=True, exist_ok=True)
    repo = SqlitePinnedMemoryRepository(str(database))
    repo.create_coremem(team_id=OLD_TEAM, fact_text="The team ships on Fridays.", capacity=10, tags=["process"], actor=AUTHOR)
    repo.create_usermem(
        canonical_user_id="usr_ana",
        fact_text="Ana prefers short answers.",
        capacity=10,
        source_key="onboarding:user_context:v1",
        actor=AUTHOR,
    )
    repo.create_usermem(canonical_user_id="usr_ben", fact_text="Ben is on call on Mondays.", capacity=10)


def _fingerprint(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


@contextmanager
def _read_only(root: Path) -> Iterator[None]:
    """Take away write access, as a ``:ro`` Docker mount does."""

    modes = {path: stat.S_IMODE(path.stat().st_mode) for path in [root, *root.rglob("*")]}
    for path, mode in modes.items():
        path.chmod(mode & ~0o222)
    try:
        yield
    finally:
        for path, mode in modes.items():
            path.chmod(mode)


def _export_read_only(old_root: Path, out: Path, caplog, *extra: str) -> PinnedMemoryExportV1:
    before = _fingerprint(old_root)
    with _read_only(old_root), caplog.at_level(logging.INFO):
        run_cli("export", "--config", str(old_root / "instance_config.yaml"), "--out", str(out), *extra)
    assert _fingerprint(old_root) == before  # not a byte changed, no file added
    return PinnedMemoryExportV1.model_validate_json(out.read_text(encoding="utf-8"))


def _import_and_check(tmp_path: Path, out: Path, capsys) -> Any:
    """Import into a new instance, check the three facts arrived, and return its store."""

    new_path, new_config = new_instance(tmp_path, "new", NEW_TEAM)
    record_people(new_config, NEW_TEAM, {"usr_new_ana": PersonStanding.ACTIVE, "usr_ben": PersonStanding.ACTIVE})
    capsys.readouterr()
    run_cli("import", "--config", str(new_path), "--in", str(out), "--map-person", "usr_ana=usr_new_ana")
    assert "Imported 3 of 3" in capsys.readouterr().out
    repo = repository(new_config)
    team = [row for row in repo.list_unexpired_facts() if row.bank == COREMEM_BANK]
    assert [(row.team_id, row.fact_text, row.tags) for row in team] == [(NEW_TEAM, "The team ships on Fridays.", ["process"])]
    ana = repo.list_usermem(canonical_user_id="usr_new_ana")
    assert [(row.fact_text, row.source_key) for row in ana] == [("Ana prefers short answers.", "onboarding:user_context:v1")]
    assert [row.fact_text for row in repo.list_usermem(canonical_user_id="usr_ben")] == ["Ben is on call on Mondays."]
    return repo


def test_export_reads_a_main_era_instance_mounted_read_only_elsewhere(tmp_path: Path, capsys, caplog) -> None:
    """Docker: the old /data volume is mounted read-only at /old next to the new instance's /data."""

    old_root = tmp_path / "old"  # recorded in its configuration as /data
    old_root.mkdir()
    shutil.copy(MAIN_ERA_CONFIG, old_root / "instance_config.yaml")
    database = old_root / "workspace" / "dbs" / "task_history.db"
    _write_old_memories(database)
    out = tmp_path / "new_data" / "memories.json"
    out.parent.mkdir()

    document = _export_read_only(old_root, out, caplog)

    printed = capsys.readouterr().out
    assert f"Read the SQLite database {database}." in printed
    assert "Wrote 3 pinned memories from instance 'old-team'" in printed
    assert (document.source_instance, document.source_team_id) == ("old-team", OLD_TEAM)
    assert "event=pinned_memory_export_root_moved recorded_root=/data" in caplog.text
    assert "event=pinned_memory_export_no_people_records" in caplog.text
    if not os.access(database.parent, os.W_OK):  # root ignores the read-only modes
        assert "mode=immutable" in caplog.text
    _import_and_check(tmp_path, out, capsys)


def _main_code(tmp_path_factory) -> Path:
    try:
        subprocess.run(
            ["git", "cat-file", "-e", f"{MAIN_ERA_COMMIT}^{{commit}}"], cwd=PROJECT_ROOT, check=True, capture_output=True
        )
    except (OSError, subprocess.CalledProcessError):
        pytest.skip(f"commit {MAIN_ERA_COMMIT[:12]} is not in this clone (a shallow checkout); fetch main to run this test")
    code = tmp_path_factory.mktemp("main_code")
    archive = subprocess.run(["git", "archive", MAIN_ERA_COMMIT], cwd=PROJECT_ROOT, check=True, capture_output=True)
    subprocess.run(["tar", "-x", "-C", str(code)], input=archive.stdout, check=True)
    return code


def test_export_reads_an_instance_written_by_main(tmp_path: Path, tmp_path_factory, capsys, caplog) -> None:
    code = _main_code(tmp_path_factory)
    written_root = tmp_path / "data"
    written_root.mkdir()
    env = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "POSTGRES_DSN", "STORAGE_BACKEND"}}
    result = subprocess.run(
        [sys.executable, "-c", MAIN_WRITER, str(written_root)],
        cwd=code,
        env={**env, "PYTHONPATH": str(code)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr[-3000:]
    database = Path(json.loads(result.stdout.strip().splitlines()[-1])["database"])
    assert database == written_root / "workspace" / "dbs" / "task_history.db"  # main's layout
    main_config = yaml.safe_load((written_root / "instance_config.yaml").read_text(encoding="utf-8"))
    assert main_config["schema_version"] == 9 and "tool_policy" in main_config and "human_interactions" in main_config
    old_root = tmp_path / "old"
    written_root.rename(old_root)  # the files now sit where the configuration does not say
    out = tmp_path / "memories.json"

    document = _export_read_only(old_root, out, caplog)

    assert f"Read the SQLite database {old_root / 'workspace' / 'dbs' / 'task_history.db'}." in capsys.readouterr().out
    assert sorted(fact.fact_text for fact in document.facts) == [
        "Ana prefers short answers.",
        "Ben is on call on Mondays.",
        "The team ships on Fridays.",
    ]
    _import_and_check(tmp_path, out, capsys)


def _write_yaml(path: Path, data: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_a_config_yaml_era_file_uses_its_workspace_and_the_old_environment_overrides(tmp_path: Path) -> None:
    root = tmp_path / "dev"
    config = _write_yaml(
        root / "config.yaml",
        {
            "schema_version": 7,
            "agent": {"workspace_dir": "."},
            "workspace": {"skills_dir": "skills", "llm_override_path": "LLM_OVERRIDES.yaml"},
            "tool_policy": {"allow_shell_commands": True},
            "storage": {"backend": "sqlite"},
        },
    )
    (root / "dbs").mkdir()
    (root / "dbs" / "task_history.db").touch()

    source = resolve_export_source(SourceOptions(config_path=str(config)), env={})
    assert (source.sqlite_path, source.instance_name) == (root / "dbs" / "task_history.db", "dev")

    with pytest.raises(SystemExit, match=str(root / "other.db")):
        resolve_export_source(SourceOptions(config_path=str(config)), env={"SQLITE_DB_PATH": "other.db"})

    dsn = "postgresql://ua:hunter2@db/ua"
    source = resolve_export_source(
        SourceOptions(config_path=str(config)), env={"STORAGE_BACKEND": "postgresql", "POSTGRES_DSN": dsn}
    )
    assert (source.backend, source.postgres_dsn) == ("postgres", dsn)
    assert "hunter2" not in repr(source) and "hunter2" not in source.describe()


def test_the_postgres_connection_comes_from_the_environment_only_for_postgres(tmp_path: Path) -> None:
    sqlite_root, pg_root = tmp_path / "lite", tmp_path / "pg"
    sqlite_config = _write_yaml(
        sqlite_root / "instance_config.yaml",
        {"schema_version": 9, "instance_name": "lite", "instance_root": str(sqlite_root), "state": {"storage": {}}},
    )
    (sqlite_root / "workspace" / "dbs").mkdir(parents=True)
    (sqlite_root / "workspace" / "dbs" / "task_history.db").touch()
    stray = {"POSTGRES_DSN_FILE": str(tmp_path / "missing")}
    assert resolve_export_source(SourceOptions(config_path=str(sqlite_config)), env=stray).backend == "sqlite"

    pg_config = _write_yaml(
        pg_root / "instance_config.yaml",
        {
            "schema_version": 9,
            "instance_name": "pg",
            "instance_root": str(pg_root),
            "state": {"storage": {"backend": "postgresql", "postgres": {"schema_name": "ua"}}},
        },
    )
    dsn_file = tmp_path / "postgres_dsn"
    dsn_file.write_text("postgresql://ua:pw@db/ua\n", encoding="utf-8")
    source = resolve_export_source(SourceOptions(config_path=str(pg_config)), env={"POSTGRES_DSN_FILE": str(dsn_file)})
    assert (source.backend, source.postgres_dsn, source.instance_name) == ("postgres", "postgresql://ua:pw@db/ua", "pg")
    with pytest.raises(SystemExit, match="no connection string was found"):
        resolve_export_source(SourceOptions(config_path=str(pg_config)), env={})


def test_the_database_is_looked_for_in_the_old_and_the_current_layout_and_never_guessed(tmp_path: Path) -> None:
    root = tmp_path / "inst"
    config = _write_yaml(
        root / "instance_config.yaml",
        {
            "schema_version": 9,
            "instance_name": "inst",
            "instance_root": str(root),
            "state": {"storage": {"backend": "sqlite", "sqlite": {"path": "dbs/task_history.db"}}},
            "authorization": {"engine": "cedar", "emergency_deny": {}},
        },
    )
    old_place = root / "workspace" / "dbs" / "task_history.db"
    new_place = root / "dbs" / "task_history.db"
    for path in (old_place, new_place):
        path.parent.mkdir(parents=True)
        path.touch()
    options = SourceOptions(config_path=str(config))

    with pytest.raises(SystemExit, match="more than one place"):
        resolve_export_source(options, env={})
    old_place.unlink()
    assert resolve_export_source(options, env={}).sqlite_path == new_place
    new_place.unlink()
    with pytest.raises(SystemExit) as refused:
        resolve_export_source(options, env={})
    message = str(refused.value)
    assert str(old_place) in message and str(new_place) in message and "--sqlite" in message


def test_export_with_only_the_database_takes_the_team_from_the_stored_rows(tmp_path: Path, capsys) -> None:
    """No configuration at all: the team id comes from the team memory, the unknown name is null."""

    database = tmp_path / "copy" / "task_history.db"
    _write_old_memories(database)
    out = tmp_path / "memories.json"

    run_cli("export", "--sqlite", str(database), "--out", str(out))

    printed = capsys.readouterr().out
    assert f"Team id: {OLD_TEAM} (found in the stored team memory)." in printed
    assert "from an instance with no recorded name" in printed
    raw = json.loads(out.read_text(encoding="utf-8"))
    assert raw["source_instance"] is None and raw["source_team_id"] == OLD_TEAM
    imported = _import_and_check(tmp_path, out, capsys).list_unexpired_facts()
    assert all("instance" not in row.metadata["imported_from"] for row in imported)


def test_several_teams_need_team_id_and_no_team_memory_records_null(tmp_path: Path, capsys) -> None:
    database = tmp_path / "task_history.db"
    _write_old_memories(database)
    SqlitePinnedMemoryRepository(str(database)).create_coremem(team_id="team_other", fact_text="Other team.", capacity=10)
    out = tmp_path / "memories.json"

    with pytest.raises(SystemExit, match="several teams: team_old, team_other. Pass --team-id"):
        run_cli("export", "--sqlite", str(database), "--out", str(out))
    with pytest.raises(SystemExit, match="no team memory for --team-id team_typo"):
        run_cli("export", "--sqlite", str(database), "--team-id", "team_typo", "--out", str(out))
    with pytest.raises(SystemExit, match="Name the old database with --sqlite or --postgres-dsn-file"):
        run_cli("export", "--team-id", OLD_TEAM, "--out", str(out))
    assert not out.exists()

    run_cli("export", "--sqlite", str(database), "--team-id", "team_other", "--instance-name", "old", "--out", str(out))
    document = PinnedMemoryExportV1.model_validate_json(out.read_text(encoding="utf-8"))
    assert [fact.fact_text for fact in document.facts if fact.bank == COREMEM_BANK] == ["Other team."]
    assert (document.source_instance, document.source_team_id) == ("old", "team_other")
    assert "Team id: team_other (given with --team-id)." in capsys.readouterr().out

    people_only = tmp_path / "people_only.db"
    SqlitePinnedMemoryRepository(str(people_only)).create_usermem(canonical_user_id="usr_ana", fact_text="Only mine.", capacity=10)
    run_cli("export", "--sqlite", str(people_only), "--out", str(tmp_path / "people.json"))
    assert json.loads((tmp_path / "people.json").read_text(encoding="utf-8"))["source_team_id"] is None
    assert "Team id: none (unknown: the database holds no team memory)." in capsys.readouterr().out


def test_a_database_without_pinned_memories_is_refused(tmp_path: Path) -> None:
    other = tmp_path / "gateway_state.db"
    import sqlite3

    with sqlite3.connect(other) as conn:
        conn.execute("CREATE TABLE gateway_users (id TEXT)")

    with pytest.raises(SystemExit, match="has no pinned memories table"):
        run_cli(
            "export", "--sqlite", str(other), "--team-id", OLD_TEAM, "--instance-name", "old",
            "--out", str(tmp_path / "m.json"),
        )


@pytest.mark.live
@pytest.mark.postgres
def test_export_reads_postgres_through_a_connection_file(tmp_path: Path) -> None:
    from storage.postgres_connection import connect_postgres
    from storage.pinned_memory_repository import PostgresPinnedMemoryRepository

    dsn = os.environ.get("LIVE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("LIVE_TEST_POSTGRES_DSN required")
    team, person = f"team_export_{uuid4().hex[:8]}", f"usr_export_{uuid4().hex[:8]}"
    repo = PostgresPinnedMemoryRepository(dsn)
    repo.create_coremem(team_id=team, fact_text="Postgres team fact.", capacity=10)
    repo.create_usermem(canonical_user_id=person, fact_text="Postgres person fact.", capacity=10)
    dsn_file = tmp_path / "postgres_dsn"
    dsn_file.write_text(dsn + "\n", encoding="utf-8")
    out = tmp_path / "memories.json"
    try:
        run_cli(
            "export", "--postgres-dsn-file", str(dsn_file), "--team-id", team, "--instance-name", "old",
            "--team", "--person", person, "--out", str(out),
        )
    finally:
        with connect_postgres(dsn) as conn:
            for table in ("pinned_memory_facts_active", "pinned_memory_fact_events"):
                conn.execute(f"DELETE FROM {table} WHERE team_id = %s OR canonical_user_id = %s", (team, person))

    document = PinnedMemoryExportV1.model_validate_json(out.read_text(encoding="utf-8"))
    assert sorted(fact.fact_text for fact in document.facts) == ["Postgres person fact.", "Postgres team fact."]
