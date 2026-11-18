"""The id check at the first start of 0.10 (instance_upgrades.protocol1_ids, plan decisions 56 and 98).

Covered: the caps follow the contract's patterns; each id kind at its cap passes and just
over it stops the start with what to do (the team id's line as the lead ruled: its length,
the 24-character cap, stay on 0.9.x; an agent id's line names the records that hold it,
C09); the record is written only when every id fits, and a check that passed does not run
again; an instance with nothing stored passes and its store is not created; settled
handbacks and another team's rows are not checked; a store that cannot be read stops the
start; the record is written only for an instance that can run as it is and only counts for
its team id (C30); the launcher and the Docker bundle refuse before the configuration
file's upgrade, NATS, seeding or the launcher start (C01), name a startup agent type that
does not fit with every other id (C28), and the bundle upgrades and starts once the ids
fit; a file upgraded before the check gets the backup to put back (C31), but only while no
record exists; once the check passed, a startup agent type that does not fit gets a short
refusal of its own, with no 0.9.x advice and no store read (C32); the bundle reads
PostgreSQL as its administrator and a database that does not exist yet holds nothing;
both onboarding routes cut a derived team id to fit and refuse a typed one that does not
(C29); and, with ``LIVE_TEST_POSTGRES_DSN``, the stored ids read from PostgreSQL.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml

import docker.launch_headless as launch_headless
import docker.run_bundle as run_bundle
from assistant_cli import _collect_onboarding_answers, build_parser
from assistant_gateway.config import GatewayInstanceConfig, migrate_instance_config_file
from assistant_gateway.onboarding_engine import OnboardingTemplate, normalize_onboarding_answers
from custom_messages.wire.ids import AGENT_ID, AGENT_TYPE, TEAM_ID
from instance_upgrades.protocol1_ids import (
    Protocol1IdCheckFailed,
    check_due,
    check_ids_before_upgrade,
    load_saved_for_check,
    stored_agent_ids_over_cap,
)
from storage.config import StorageSettings, resolve_storage_settings

TEAM = "team_ids"
AT_ID_CAP = "agent_" + "a" * 26  # 32 characters
OVER_ID_CAP = "agent_" + "a" * 27  # 33
AT_TYPE_CAP = "Planner" + "x" * 17  # 24
OVER_TYPE_CAP = "Planner" + "x" * 18  # 25


def _config(tmp_path: Path, *, team: str = TEAM, types: tuple[str, ...] = (),
            onboarded: bool = True) -> GatewayInstanceConfig:
    config = GatewayInstanceConfig.create(instance_name="ids", instance_root=str(tmp_path / "instance"), team_id=team,
                                          timezone="UTC")
    if types:
        config.agent.agent_definition_paths = [str(_catalog(tmp_path, types))]
    if onboarded:
        config.layout.control_dir.mkdir(parents=True, exist_ok=True)
    return config


def _catalog(tmp_path: Path, types: tuple[str, ...]) -> Path:
    (tmp_path / "prompt.md").write_text("# Planner\n\nPlan things.", encoding="utf-8")
    definitions = {
        name: {"display_name": "Planner", "role": "Planning", "description": "Plans.", "prompt_ref": "prompt.md",
               "backstory": "hub_and_spoke", "onboarding": {"visible": False}}
        for name in types
    }
    path = tmp_path / "agents.yaml"
    path.write_text(yaml.safe_dump({"schema_version": 1, "agent_types": definitions}), encoding="utf-8")
    return path


def _sqlite(tmp_path: Path) -> StorageSettings:
    return resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "team.db")}})


_SCHEMA = (
    "CREATE TABLE active_task_runtime (owner_agent_id TEXT PRIMARY KEY, team_id TEXT, runtime_state_json TEXT)",
    "CREATE TABLE active_task_runtime_records (owner_agent_id TEXT, section TEXT, record_id TEXT)",
    "CREATE TABLE agent_deferred_messages (owner_agent_id TEXT PRIMARY KEY, team_id TEXT, messages_json TEXT)",
    "CREATE TABLE runtime_task_custody (team_id TEXT, task_id TEXT, holder_agent_id TEXT)",
    "CREATE TABLE worker_task_handbacks (handback_id TEXT, team_id TEXT, worker_agent_id TEXT, status TEXT)",
)


def _store(conn: Any, bind: str, agent_id: str, *, team: str | None = TEAM, status: str = "received") -> None:
    rows = (
        ("INSERT INTO active_task_runtime VALUES ({p},{p},'{{}}')", (agent_id, team)),
        ("INSERT INTO active_task_runtime_records VALUES ({p},'tasks','t1')", (agent_id,)),
        ("INSERT INTO agent_deferred_messages VALUES ({p},{p},'[]')", (agent_id, team)),
        ("INSERT INTO runtime_task_custody VALUES ({p},'t1',{p})", (team or TEAM, agent_id)),
        ("INSERT INTO worker_task_handbacks VALUES ({p},{p},{p},{p})", ("hb-" + agent_id, team or TEAM, agent_id,
                                                                       status)),
    )
    for sql, params in rows:
        conn.execute(sql.format(p=bind), params)


def _write_sqlite(storage: StorageSettings, *agent_ids: str, wal: bool = True, **kwargs: Any) -> None:
    with sqlite3.connect(str(storage.sqlite_path)) as conn:
        if wal:
            conn.execute("PRAGMA journal_mode=WAL")
        for statement in _SCHEMA:
            conn.execute(statement.replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS"))
        for agent_id in agent_ids:
            _store(conn, "?", agent_id, **kwargs)
    conn.close()


def _real_stores(storage: StorageSettings, bind: str, *agent_ids: str) -> None:
    """The stores' own schemas (the repositories create them), with a row per id in each column checked."""

    from storage.runtime_state_repository import create_runtime_state_repository
    from storage.worker_handback_repository import WorkerHandbackRepository

    create_runtime_state_repository(storage)
    WorkerHandbackRepository(storage)
    now = "2026-11-18T12:00:00+00:00"
    p = bind
    with _open(storage) as conn:
        for agent_id in agent_ids:
            conn.execute(f"INSERT INTO active_task_runtime (owner_agent_id, team_id, runtime_state_json) "
                         f"VALUES ({p},{p},'{{}}')", (agent_id, TEAM))
            conn.execute(f"INSERT INTO active_task_runtime_records VALUES ({p},'tasks','t1','{{}}','d')", (agent_id,))
            conn.execute(f"INSERT INTO agent_deferred_messages (owner_agent_id, team_id, messages_json) "
                         f"VALUES ({p},{p},'[]')", (agent_id, TEAM))
            conn.execute(f"INSERT INTO runtime_task_custody (team_id, task_id, holder_agent_id, claimed_at) "
                         f"VALUES ({p},{p},{p},{p})", (TEAM, "task-" + agent_id, agent_id, now))
            conn.execute(f"INSERT INTO worker_task_handbacks VALUES ({p},{p},{p},{p},'interrupted','received',{p},{p},"
                         "'{}')", ("hb-" + agent_id, TEAM, "task-" + agent_id, agent_id, now, now))
        conn.commit()


def _open(storage: StorageSettings) -> Any:
    from storage.postgres_connection import connect_postgres
    from storage.sqlite_connection import connect_sqlite

    return connect_postgres(storage.postgres_dsn) if storage.postgres_dsn else connect_sqlite(str(storage.sqlite_path))


# -- the caps --------------------------------------------------------------------------------


@pytest.mark.parametrize("rule", [TEAM_ID, AGENT_TYPE, AGENT_ID], ids=lambda rule: rule.what)
def test_the_caps_follow_the_contract(rule) -> None:
    assert rule.problem("a" * rule.cap) is None
    assert rule.problem("a" * (rule.cap + 1)) == f"is {rule.cap + 1} characters long; protocol 1 allows at most {rule.cap}"
    assert "letters, digits, '-' and '_', starting with a letter or digit" in (rule.problem("_a") or "")
    assert (TEAM_ID.cap, AGENT_TYPE.cap, AGENT_ID.cap) == (24, 24, 32)  # plan decisions 56 and 98


# -- the check -------------------------------------------------------------------------------


def test_ids_at_their_caps_pass_and_the_record_is_written_once(tmp_path, monkeypatch) -> None:
    config, storage = _config(tmp_path, team="t" * 24, types=(AT_TYPE_CAP,)), _sqlite(tmp_path)
    _write_sqlite(storage, AT_ID_CAP, team="t" * 24)
    stored = Path(str(storage.sqlite_path)).read_bytes()

    assert check_ids_before_upgrade(config, lambda: storage) is None

    record = json.loads(config.layout.protocol1_ids_check_path.read_text(encoding="utf-8"))
    assert record["team_id"] == "t" * 24 and record["version"] and record["checked_at"]
    # Read only: the database is as it was; SQLite's own log is opened, as by any reader, and stays empty.
    assert Path(str(storage.sqlite_path)).read_bytes() == stored
    assert Path(str(storage.sqlite_path) + "-wal").stat().st_size == 0

    def unread(*_args: Any) -> None:
        raise AssertionError("a check that passed runs again")

    monkeypatch.setattr("instance_upgrades.protocol1_ids.catalog_definitions", unread)
    _write_sqlite(storage, OVER_ID_CAP)
    assert check_ids_before_upgrade(config, lambda: storage) is None  # later stored ids never stop a start


def test_each_id_just_over_its_cap_stops_the_start_and_says_what_to_do(tmp_path, caplog) -> None:
    long_team = "team_" + "x" * 20  # 25 characters
    config, storage = _config(tmp_path, team=long_team, types=(OVER_TYPE_CAP, AT_TYPE_CAP)), _sqlite(tmp_path)
    _write_sqlite(storage, OVER_ID_CAP, AT_ID_CAP, team=long_team)

    message = check_ids_before_upgrade(config, lambda: storage)

    assert message is not None and not config.layout.protocol1_ids_check_path.exists()
    lines = message.splitlines()
    assert "event=protocol1_ids_over_caps" in lines[0] and "This start changed nothing" in lines[0]
    assert lines[1] == (f"- The team id '{long_team}' is 25 characters long; protocol 1 allows at most 24. This "
                        "instance cannot run 0.10 with that team id: keep running 0.9.x.")
    assert lines[2].startswith(f"- The agent type '{OVER_TYPE_CAP}' (defined in {tmp_path / 'agents.yaml'}) is 25 "
                               "characters long; protocol 1 allows at most 24. On 0.9.x, rename it")
    assert ("in the file that defines it and everywhere the instance names it: gateway.startup_agents in the "
            "configuration file and the team roster (control/TEAM_AGENTS.yaml). For a specialist the team lead "
            "created, remove it and have it created again under the shorter name.") in lines[2]
    assert lines[3] == (
        f"- The agent id '{OVER_ID_CAP}' is 33 characters long; protocol 1 allows at most 32. It is stored with "
        "its running tasks, its running tasks' saved progress, messages waiting for it, the tasks it holds, tasks "
        "it handed back that the team lead has not dealt with yet. On 0.9.x, let that agent's tasks finish or stop "
        "them, and let the team lead deal with tasks it handed back, so these records go. Then start this version "
        "again."
    )
    assert len(lines) == 4  # the ids at their caps are not named
    assert f"event=protocol1_ids_over_caps team_id={long_team}" in caplog.text


def test_the_check_reads_the_stores_own_schemas(tmp_path) -> None:
    storage = _sqlite(tmp_path)
    _real_stores(storage, "?", OVER_ID_CAP, AT_ID_CAP)

    [found] = stored_agent_ids_over_cap(storage, TEAM)

    assert found.value == OVER_ID_CAP and len(found.found_in) == 5


def test_an_instance_with_nothing_stored_passes_and_its_store_is_not_created(tmp_path) -> None:
    storage = _sqlite(tmp_path)

    assert check_ids_before_upgrade(_config(tmp_path), lambda: storage) is None
    assert not Path(str(storage.sqlite_path)).exists()
    sqlite3.connect(str(storage.sqlite_path)).close()  # a database without the tables
    assert stored_agent_ids_over_cap(storage, TEAM) == []
    with sqlite3.connect(str(storage.sqlite_path)) as conn:
        assert conn.execute("SELECT count(*) FROM sqlite_master").fetchone() == (0,)
    conn.close()


def test_settled_handbacks_and_another_teams_rows_are_not_checked(tmp_path) -> None:
    storage = _sqlite(tmp_path)
    with sqlite3.connect(str(storage.sqlite_path)) as conn:
        for statement in _SCHEMA:
            conn.execute(statement)
        conn.execute("INSERT INTO worker_task_handbacks VALUES ('hb1', ?, ?, 'redispatched')", (TEAM, OVER_ID_CAP))
        conn.execute("INSERT INTO worker_task_handbacks VALUES ('hb2', ?, ?, 'closed_finished')", (TEAM, OVER_ID_CAP))
        other = "other_" + "b" * 27
        conn.execute("INSERT INTO active_task_runtime VALUES (?, 'team_other', '{}')", (other,))
        conn.execute("INSERT INTO active_task_runtime_records VALUES (?, 'tasks', 't1')", (other,))
        conn.execute("INSERT INTO runtime_task_custody VALUES ('team_other', 't1', ?)", (other,))
        unteamed = "unteamed_" + "c" * 24
        conn.execute("INSERT INTO agent_deferred_messages VALUES (?, NULL, '[]')", (unteamed,))
    conn.close()

    [found] = stored_agent_ids_over_cap(storage, TEAM)

    assert found.value == unteamed and found.found_in == ("messages waiting for it",)  # no team id: checked


def test_a_store_that_cannot_be_read_stops_the_start(tmp_path) -> None:
    storage = _sqlite(tmp_path)
    Path(str(storage.sqlite_path)).write_bytes(b"not a database" * 100)

    with pytest.raises(Protocol1IdCheckFailed, match="The stored records could not be read"):
        stored_agent_ids_over_cap(storage, TEAM)
    refusal = launch_headless.protocol1_id_check_refusal(_config(tmp_path), lambda: storage)
    assert refusal is not None and "event=protocol1_ids_check_failed" in refusal


# -- the launchers refuse before anything changes (C01) -----------------------------------------


def test_a_team_whose_import_did_not_finish_is_not_read(tmp_path, monkeypatch) -> None:
    config = _config(tmp_path, team="team_" + "y" * 25)
    marker = config.layout.team_import_marker_path
    marker.parent.mkdir(parents=True)
    marker.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("instance_upgrades.protocol1_ids.catalog_definitions",
                        lambda *_args: pytest.fail("a half-imported team was read"))

    refusal = launch_headless.protocol1_id_check_refusal(config, lambda: _sqlite(tmp_path))

    assert refusal is not None and refusal.startswith("event=team_import_unfinished The team import into")
    assert not config.layout.protocol1_ids_check_path.exists()


# -- the record (C30) ----------------------------------------------------------------------------


def test_the_record_is_written_only_for_an_instance_that_can_run_as_it_is(tmp_path, caplog) -> None:
    """Onboarding writes the team id and the layout's moves bring the task database to where the check reads."""

    caplog.set_level(logging.INFO, logger="instance_upgrades.protocol1_ids")
    config, storage = _config(tmp_path, onboarded=False), _sqlite(tmp_path)
    record = config.layout.protocol1_ids_check_path

    assert check_ids_before_upgrade(config, lambda: storage) is None
    assert not record.exists()
    assert "the instance is not onboarded yet" in caplog.text and "event=protocol1_ids_checked_not_recorded" in caplog.text

    config.layout.control_dir.mkdir(parents=True)
    (config.instance_root_path / "workspace").mkdir()
    (config.instance_root_path / "workspace" / "notes.md").write_text("old layout", encoding="utf-8")
    assert check_ids_before_upgrade(config, lambda: storage) is None
    assert not record.exists() and "still in the old layout" in caplog.text

    (config.instance_root_path / "workspace" / "notes.md").unlink()
    assert check_ids_before_upgrade(config, lambda: storage) is None
    assert json.loads(record.read_text(encoding="utf-8"))["team_id"] == TEAM


def test_a_record_for_another_team_id_or_one_that_cannot_be_read_does_not_count(tmp_path, caplog) -> None:
    caplog.set_level(logging.INFO, logger="instance_upgrades.protocol1_ids")
    config, storage = _config(tmp_path), _sqlite(tmp_path)
    record = config.layout.protocol1_ids_check_path
    record.parent.mkdir(parents=True)
    _write_sqlite(storage, OVER_ID_CAP)

    record.write_text(json.dumps({"team_id": "team_other"}), encoding="utf-8")
    assert check_due(config)  # says nothing itself: the check logs why, once per start
    assert OVER_ID_CAP in (check_ids_before_upgrade(config, lambda: storage) or "")
    assert caplog.text.count("recorded_team_id=team_other team_id=team_ids") == 1

    for unreadable in ("{not json", "[]"):
        caplog.clear()
        record.write_text(unreadable, encoding="utf-8")
        assert check_due(config)
        assert OVER_ID_CAP in (check_ids_before_upgrade(config, lambda: storage) or "")
        assert caplog.text.count("event=protocol1_ids_record_unreadable") == 1


def test_the_launcher_leaves_no_record_for_an_instance_it_then_refuses(tmp_path, monkeypatch, caplog) -> None:
    path = _version_12_file(tmp_path, team=TEAM, onboarded=False)
    monkeypatch.setattr(sys, "argv", ["launch_headless.py", "--config", str(path)])

    assert launch_headless.main() == 2

    assert "Team control folder is missing" in caplog.text and "Run `onboard`" in caplog.text
    assert not load_saved_for_check(path).layout.protocol1_ids_check_path.exists()


# -- the launchers refuse before anything changes (C01) -----------------------------------------


def _version_12_file(tmp_path: Path, *, team: str, types: tuple[str, ...] = (), startup: tuple[str, ...] = (),
                     onboarded: bool = True) -> Path:
    """A saved instance configuration as 0.9 wrote it: schema version 12, no queue settings.

    ``startup`` adds ``gateway.startup_agents`` entries, written into the file since this
    version's configuration refuses a type protocol 1 cannot carry.
    """

    path = _config(tmp_path, team=team, types=types, onboarded=onboarded).save()
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["schema_version"] = 12
    data.get("transport", {}).pop("queues", None)
    data["gateway"]["startup_agents"] += [{"named_agent_type": name, "count": 1, "enabled": True} for name in startup]
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def _unchanged(path: Path, before: bytes) -> None:
    assert path.read_bytes() == before
    assert list(path.parent.glob(f"{path.name}.bak*")) == []
    assert not load_saved_for_check(path).layout.protocol1_ids_check_path.exists()


def test_the_launcher_refuses_a_long_team_id_before_it_upgrades_the_file(tmp_path, monkeypatch, caplog) -> None:
    path = _version_12_file(tmp_path, team="team_" + "y" * 25)
    before = path.read_bytes()
    monkeypatch.setattr(sys, "argv", ["launch_headless.py", "--config", str(path)])
    monkeypatch.setattr(launch_headless, "ensure_instance_config_ready_for_headless_launch",
                        lambda _path: pytest.fail("the file was upgraded"))

    assert launch_headless.main() == 2

    _unchanged(path, before)
    assert "Headless launch refused." in caplog.text and "is 30 characters long" in caplog.text


class _Started:
    """Fakes for what the bundle starts after the check, recording each start."""

    def __init__(self, monkeypatch) -> None:
        self.calls: list[str] = []
        record = self.calls.append

        class _Exited:
            def poll(self) -> int:
                return 0

        monkeypatch.setattr(run_bundle, "start_postgres", lambda: pytest.fail("PostgreSQL started for SQLite"))
        monkeypatch.setattr(run_bundle, "start_local_nats", lambda **_kwargs: record("nats") or _Exited())
        monkeypatch.setattr(run_bundle, "seed_postgres_from_targets", lambda targets: record("seed"))
        monkeypatch.setattr(run_bundle.subprocess, "Popen", lambda *args, **kwargs: record("launcher") or _Exited())
        monkeypatch.setattr(run_bundle, "terminate_process", lambda process, name: None)


def _run_bundle(path: Path) -> int:
    return run_bundle.run_supervised_bundle(config=GatewayInstanceConfig.load_saved(path), runtime_env={},
                                            env_path=None, gateway_args=[])


def test_the_bundle_refuses_a_long_team_id_before_it_upgrades_the_file_or_starts_anything(
        tmp_path, monkeypatch, caplog) -> None:
    path = _version_12_file(tmp_path, team="team_" + "y" * 25)
    before = path.read_bytes()
    started = _Started(monkeypatch)

    assert _run_bundle(path) == 2

    _unchanged(path, before)
    assert started.calls == []
    assert "Bundle launch refused." in caplog.text and "keep running 0.9.x" in caplog.text


def test_the_bundle_leaves_the_file_to_the_check_before_postgresql_starts(tmp_path, monkeypatch) -> None:
    """The bundle's main reads the saved file upgraded in memory only; run_supervised_bundle upgrades it."""

    path = _version_12_file(tmp_path, team="team_" + "y" * 25)
    before = path.read_bytes()
    handed: list[GatewayInstanceConfig] = []
    monkeypatch.setenv("TEAM_BUNDLE_SERVICE_ROOT", str(tmp_path / "services"))
    monkeypatch.setattr(sys, "argv", ["run_bundle.py", "--config", str(path)])
    monkeypatch.setattr(run_bundle, "run_supervised_bundle", lambda **kwargs: handed.append(kwargs["config"]) or 7)

    assert run_bundle.main() == 7

    assert path.read_bytes() == before
    assert handed[0].manager.team_id == "team_" + "y" * 25 and handed[0].transport.queues.partitions == 96


def test_the_bundle_upgrades_the_file_and_starts_once_the_ids_fit(tmp_path, monkeypatch) -> None:
    path = _version_12_file(tmp_path, team=TEAM)
    started = _Started(monkeypatch)

    assert _run_bundle(path) == 0

    assert started.calls == ["nats", "seed", "launcher"]
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["schema_version"] == 15
    assert list(path.parent.glob(f"{path.name}.bak*"))
    assert GatewayInstanceConfig.load_saved(path).layout.protocol1_ids_check_path.exists()

    monkeypatch.setattr(run_bundle, "id_check_storage", lambda *_args: pytest.fail("checked again"))
    assert _run_bundle(path) == 0  # a later start: the store is not read again


def test_both_launchers_name_a_startup_agent_type_that_does_not_fit_with_every_other_id(
        tmp_path, monkeypatch, caplog) -> None:
    """The configuration refuses such a type, so the check sets it aside to read the rest (C28)."""

    long_team, started_only = "team_" + "y" * 25, "Started" + "z" * 18
    path = _version_12_file(tmp_path, team=long_team, types=(OVER_TYPE_CAP,), startup=(OVER_TYPE_CAP, started_only))
    before = path.read_bytes()
    with pytest.raises(ValueError, match=f"The startup agent type '{OVER_TYPE_CAP}' is 25 characters long"):
        GatewayInstanceConfig.load_saved(path)
    expected = (
        f"- The team id '{long_team}' is 30 characters long",
        f"- The agent type '{OVER_TYPE_CAP}' (defined in {tmp_path / 'agents.yaml'}; started by "
        f"gateway.startup_agents in {path}) is 25 characters long; protocol 1 allows at most 24. On 0.9.x, rename it",
        f"- The agent type '{started_only}' (started by gateway.startup_agents in {path}) is 25 characters long",
    )

    monkeypatch.setattr(sys, "argv", ["launch_headless.py", "--config", str(path)])
    assert launch_headless.main() == 2
    _unchanged(path, before)
    assert "Headless launch refused." in caplog.text and all(line in caplog.text for line in expected)
    assert "already upgraded" not in caplog.text  # a version-12 file 0.9.x still reads

    caplog.clear()
    started = _Started(monkeypatch)
    monkeypatch.setenv("TEAM_BUNDLE_SERVICE_ROOT", str(tmp_path / "services"))
    monkeypatch.setattr(sys, "argv", ["run_bundle.py", "--config", str(path)])
    assert run_bundle.main() == 2
    _unchanged(path, before)
    assert started.calls == []
    assert "Bundle launch refused." in caplog.text and all(line in caplog.text for line in expected)


def test_the_bundle_refuses_settings_it_cannot_read_without_a_traceback(tmp_path, monkeypatch, caplog) -> None:
    path = _version_12_file(tmp_path, team=TEAM)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["gateway"]["startup_agents"][0]["count"] = 99
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["run_bundle.py", "--config", str(path)])
    monkeypatch.setattr(run_bundle, "run_supervised_bundle", lambda **_kwargs: pytest.fail("the bundle started"))

    assert run_bundle.main() == 2

    assert f"Bundle launch refused. The settings in {path} cannot be read" in caplog.text
    assert "count must be between 1 and 32" in caplog.text


def test_a_file_upgraded_before_the_check_gets_the_backup_to_put_back(tmp_path) -> None:
    """migrate-config runs no id check; 0.9.x cannot read the file it writes (C31).

    Named only while no record exists: a record of any kind, for another team id or one that
    cannot be read, means this version started here, and its stores go forward only (C32).
    """

    path = _version_12_file(tmp_path, team="team_" + "y" * 25)
    backup = migrate_instance_config_file(path).backup_path
    config, storage = load_saved_for_check(path), _sqlite(tmp_path)

    lines = (check_ids_before_upgrade(config, lambda: storage) or "").splitlines()

    assert lines[1] == (
        f"The configuration file {path} was already upgraded to schema_version 15 for this version, which 0.9.x "
        f"cannot read. Before starting 0.9.x again, put back {path}.bak.v12 as instance_config.yaml."
    )
    assert backup == path.with_name("instance_config.yaml.bak.v12")
    record = config.layout.protocol1_ids_check_path
    record.parent.mkdir(parents=True)
    for written in (json.dumps({"team_id": "team_other"}), "{not json"):
        record.write_text(written, encoding="utf-8")
        refusal = check_ids_before_upgrade(config, lambda: storage) or ""
        assert "is 30 characters long" in refusal and "put back" not in refusal
    record.unlink()
    backup.unlink()  # as for a file this version created: it never ran on 0.9.x
    assert "already upgraded" not in (check_ids_before_upgrade(config, lambda: storage) or "")


def test_once_the_check_passed_a_startup_type_that_does_not_fit_gets_a_short_refusal_in_both_launchers(
        tmp_path, monkeypatch, caplog) -> None:
    """0.10 runs here, so the refusal names only the startup entry, with no 0.9.x advice and no store read (C32).

    The critic's sequence: the check passed and the launcher upgraded the file (keeping
    instance_config.yaml.bak.v12); then a startup agent with a long type is added to the
    file by hand and a long type to the catalog, which never stops a start of its own.
    """

    path = _version_12_file(tmp_path, team=TEAM, types=(AT_TYPE_CAP,))
    _Started(monkeypatch)
    assert _run_bundle(path) == 0
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["schema_version"] == 15
    assert path.with_name("instance_config.yaml.bak.v12").exists()
    started_only = "Started" + "z" * 18
    _catalog(tmp_path, (AT_TYPE_CAP, OVER_TYPE_CAP))
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["gateway"]["startup_agents"].append({"named_agent_type": started_only, "count": 1, "enabled": True})
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    before = path.read_bytes()
    for reader in ("catalog_definitions", "stored_agent_ids_over_cap"):
        monkeypatch.setattr(f"instance_upgrades.protocol1_ids.{reader}",
                            lambda *_args, _reader=reader: pytest.fail(f"{_reader} ran"))
    monkeypatch.setattr(run_bundle, "id_check_storage", lambda *_args: pytest.fail("the store was found"))
    expected = (
        f"This instance cannot start: gateway.startup_agents in {path} starts agent types that protocol 1 cannot "
        "carry in message addresses (event=protocol1_startup_agent_types_over_cap). This start changed nothing.\n"
        f"- The agent type '{started_only}' is 25 characters long; protocol 1 allows at most 24. Remove its entry "
        "from gateway.startup_agents, or change it to an agent type of at most 24 letters, digits, '-' and '_' (a "
        "type you rename, rename where it is defined as well). Then start again."
    )

    monkeypatch.setattr(sys, "argv", ["launch_headless.py", "--config", str(path)])
    assert launch_headless.main() == 2
    assert f"Headless launch refused. {expected}" in caplog.text

    caplog.clear()
    started = _Started(monkeypatch)
    monkeypatch.setenv("TEAM_BUNDLE_SERVICE_ROOT", str(tmp_path / "services"))
    monkeypatch.setattr(sys, "argv", ["run_bundle.py", "--config", str(path)])
    assert run_bundle.main() == 2
    assert started.calls == []
    assert f"Bundle launch refused. {expected}" in caplog.text

    for text in ("0.9.x", "put back", OVER_TYPE_CAP):
        assert text not in caplog.text
    assert path.read_bytes() == before
    assert json.loads(load_saved_for_check(path).layout.protocol1_ids_check_path.read_text(encoding="utf-8"))


def test_the_bundle_reads_postgresql_as_its_administrator_and_a_missing_database_holds_nothing(
        tmp_path, monkeypatch) -> None:
    config = _config(tmp_path)
    config.state.storage.backend = "postgres"
    config.state.storage.postgres.dsn = "postgresql://team:secret@db.example:6543/team_agents"
    databases: set[str] = set()
    asked: list[str] = []

    class _Connection:
        def __enter__(self) -> _Connection:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def execute(self, _sql: str, params: tuple[str]) -> Any:
            return type("Rows", (), {"fetchone": lambda _self: (1,) if params[0] in databases else None})()

    def connect(dsn: str, autocommit: bool) -> _Connection:
        asked.append(dsn)
        return _Connection()

    monkeypatch.setitem(sys.modules, "psycopg", type(sys)("psycopg"))
    monkeypatch.setattr(sys.modules["psycopg"], "connect", connect, raising=False)

    assert run_bundle.id_check_storage(config, {}) is None  # no database yet: nothing stored
    databases.add("team_agents")
    storage = run_bundle.id_check_storage(config, {})
    assert storage is not None and storage.postgres_dsn == "postgresql://postgres@127.0.0.1:5432/team_agents"
    assert asked == ["postgresql://postgres@127.0.0.1:5432/postgres"] * 2


# -- onboarding --------------------------------------------------------------------------------


def _team_id(**answers: str) -> str:
    return normalize_onboarding_answers(answers, OnboardingTemplate(team_id="team_template"))["team_id"]


def test_onboarding_cuts_a_derived_team_id_to_fit_and_refuses_a_typed_one_that_does_not() -> None:
    assert _team_id(assistant_name="The Very Long Assistant Name Of Ours") == "team_the_very_long_assis"
    assert _team_id(assistant_name="Ada Lovelace Helper") == "team_ada_lovelace_helper"  # 24: kept whole
    assert _team_id(assistant_name="Ada Lovelace Helpe Desk") == "team_ada_lovelace_helpe"  # cut; no trailing '_'
    assert _team_id(team_id="team_ours") == "team_ours"
    with pytest.raises(ValueError, match="The team id 'team_with_a_much_too_long_name' is 30 characters long"):
        _team_id(team_id="team_with_a_much_too_long_name")
    with pytest.raises(ValueError, match="has characters protocol 1 does not allow"):
        _team_id(team_id="team ours")


@pytest.mark.parametrize(("team_id", "problem"), [("team_" + "x" * 20, "is 25 characters long"),
                                                  ("team ours", "has characters protocol 1 does not allow")])
def test_the_command_line_onboarding_refuses_a_team_id_that_does_not_fit(tmp_path, monkeypatch, capsys, team_id,
                                                                         problem) -> None:
    """Without prompts it exits with the reason; at the prompt it asks again (C29)."""

    monkeypatch.chdir(tmp_path)
    template = OnboardingTemplate(team_id="team_template")
    args = build_parser().parse_args(["onboard", "--instance-name", "ids", "--non-interactive", "--team-id", team_id])
    with pytest.raises(SystemExit, match=f"The team id '{team_id}' {problem}"):
        _collect_onboarding_answers(args, template=template)

    typed = iter([team_id, "team_ours"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(typed) if prompt.startswith("Team id") else "")
    answers = _collect_onboarding_answers(build_parser().parse_args(["onboard", "--instance-name", "ids"]),
                                          template=template)

    assert answers["team_id"] == "team_ours"
    assert f"The team id '{team_id}' {problem}" in capsys.readouterr().out


# -- a real PostgreSQL ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.live
@pytest.mark.postgres
def test_stored_ids_are_read_from_postgresql_without_creating_anything() -> None:
    from psycopg.conninfo import make_conninfo

    from storage.postgres_connection import connect_postgres

    dsn = os.environ.get("LIVE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("LIVE_TEST_POSTGRES_DSN is required for the PostgreSQL id check")
    schema = "ids_" + uuid4().hex
    with connect_postgres(dsn) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
    in_schema = resolve_storage_settings(
        {"backend": "postgres", "postgres": {"dsn": make_conninfo(dsn, options=f"-csearch_path={schema}")}})
    try:
        assert stored_agent_ids_over_cap(in_schema, TEAM) == []  # no tables: nothing stored, none created
        with connect_postgres(in_schema.postgres_dsn) as conn:
            assert conn.execute("SELECT count(*) FROM pg_tables WHERE schemaname = %s", (schema,)).fetchone()[0] == 0
        _real_stores(in_schema, "%s", OVER_ID_CAP, AT_ID_CAP)
        with connect_postgres(in_schema.postgres_dsn) as conn:  # another team's agent in the same database
            conn.execute("INSERT INTO active_task_runtime (owner_agent_id, team_id, runtime_state_json) "
                         "VALUES (%s, 'team_other', '{}')", ("other_" + "b" * 27,))

        [found] = stored_agent_ids_over_cap(in_schema, TEAM)

        assert found.value == OVER_ID_CAP and len(found.found_in) == 5
    finally:
        with connect_postgres(dsn) as conn:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")
