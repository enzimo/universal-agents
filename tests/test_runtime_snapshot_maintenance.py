"""Exercise upgrade retirement against real SQLite and PostgreSQL storage."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from storage import runtime_snapshot_maintenance as maintenance
from storage.config import resolve_storage_settings
from storage.postgres_connection import connect_postgres
from storage.runtime_state_repository import create_runtime_state_repository
from storage.runtime_snapshot_layout import payload_digest
from custom_messages.contracts.runtime_snapshot_v3 import decode_runtime_snapshot
from tests.runtime_snapshot_fixtures import snapshot_json


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.live)])
def seeded_store(request, tmp_path):
    schema = "snapshot_retirement_" + uuid4().hex
    if request.param == "postgres":
        dsn = os.getenv("LIVE_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip("LIVE_TEST_POSTGRES_DSN required")
        from psycopg.conninfo import make_conninfo

        with connect_postgres(dsn) as conn:
            conn.execute(f"CREATE SCHEMA {schema}")
        settings = resolve_storage_settings(
            {
                "backend": "postgres",
                "postgres": {
                    "dsn": make_conninfo(dsn, options=f"-c search_path={schema}"),
                },
            }
        )
    else:
        settings = resolve_storage_settings(
            {"backend": "sqlite", "sqlite": {"path": str(tmp_path / "runtime.db")}}
        )
    repo = create_runtime_state_repository(settings)
    # The previous writer stored the entire wire-v3 document in this column.
    # Bootstrapping the new repository adds the record table but leaves it intact.
    with maintenance._connection(settings) as conn:
        bind = conn.dialect.bind
        for owner, team in (
            ("old_a", "nova"),
            ("old_b", "nova"),
            ("other_team", "other"),
        ):
            conn.execute(
                f"INSERT INTO active_task_runtime VALUES ({', '.join([bind] * 6)})",
                (
                    owner,
                    "Manager",
                    team,
                    "TeamArchitect",
                    snapshot_json(owner),
                    "2026-09-08T00:00:00Z",
                ),
            )
        # Also retain opaque child records even when their parent is unsupported.
        conn.execute(
            f"INSERT INTO active_task_runtime_records VALUES ({', '.join([bind] * 5)})",
            ("old_a", "tasks", "opaque", '{"private":"payload"}', "original_digest"),
        )
    repo.save_task_runtime(
        owner_agent_id="current",
        owner_agent_name="Manager",
        team_id="nova",
        owner_named_agent_type="TeamArchitect",
        runtime_state_json=snapshot_json("current_work"),
    )
    repo.save_deferred_messages(
        owner_agent_id="old_a",
        owner_agent_name="Manager",
        team_id="nova",
        owner_named_agent_type="TeamArchitect",
        messages_json="[]",
    )
    repo.record_task_cancellations(
        team_id="nova",
        root_task_id="cancelled",
        task_ids=["cancelled"],
        canonical_user_id=None,
        command_id="stop",
        reason="stopped",
        cancelled_at="2026-09-08T00:00:00Z",
    )
    yield settings, repo
    if request.param == "postgres":
        with connect_postgres(dsn) as conn:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")


def raw_rows(settings, table):
    with maintenance._connection(settings) as conn:
        return [
            dict(row)
            for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2").fetchall()
        ]


def seed_old_creation_result(settings, repo):
    """Reproduce a record the current contract rejects inside a supported storage layout.

    Until 2026-10-04 this used the delegation assurance section's old string
    result; that section was removed with delegation assurance, so a pause
    record carrying a field the contract no longer has stands in for it.
    """
    document = json.loads(snapshot_json("delegating_work"))
    document["recoverable_pause_records"] = {
        "pause": {
            "task_id": "delegating_work",
            "pause_id": "pause",
            "trigger": "tool_call_limit",
        }
    }
    repo.save_task_runtime(
        owner_agent_id="old_result",
        owner_agent_name="Lead",
        team_id="nova",
        owner_named_agent_type="TeamArchitect",
        runtime_state_json=json.dumps(document),
    )
    record = document["recoverable_pause_records"]["pause"]
    record["last_creation_result"] = "SUCCESS: private-creation-result"
    payload = json.dumps(record)
    with maintenance._connection(settings) as conn:
        bind = conn.dialect.bind
        conn.execute(
            f"UPDATE active_task_runtime_records SET payload_json={bind}, payload_digest={bind} "
            f"WHERE owner_agent_id={bind} AND section={bind}",
            (
                payload,
                payload_digest(payload),
                "old_result",
                "recoverable_pause_records",
            ),
        )


def test_current_layout_with_old_result_is_previewed_and_retired(seeded_store):
    settings, repo = seeded_store
    seed_old_creation_result(settings, repo)
    rows = repo.load_task_runtime_by_owner("old_result")
    with pytest.raises(ValueError, match="last_creation_result"):
        decode_runtime_snapshot(rows[0].runtime_state_json)
    before = raw_rows(settings, "active_task_runtime")
    records = raw_rows(settings, "active_task_runtime_records")
    preview = maintenance.inspect_runtime_snapshots(settings, team_id="nova")
    assert {row["owner_agent_id"] for row in preview} == {
        "old_a",
        "old_b",
        "old_result",
    }
    assert "private-creation-result" not in json.dumps(preview)
    assert raw_rows(settings, "active_task_runtime") == before
    assert raw_rows(settings, "active_task_runtime_records") == records

    maintenance.retire_runtime_snapshots(
        settings,
        team_id="nova",
        owner_agent_ids=["old_result"],
        reason="Explicitly abandon old delegation",
    )
    archive = raw_rows(settings, maintenance.ARCHIVE_TABLE)[0]
    assert json.loads(archive["records_json"]) == [
        row for row in records if row["owner_agent_id"] == "old_result"
    ]
    assert (
        archive["runtime_state_json"]
        == next(row for row in before if row["owner_agent_id"] == "old_result")[
            "runtime_state_json"
        ]
    )
    assert not repo.load_task_runtime_by_owner("old_result")
    assert (
        decode_runtime_snapshot(
            repo.load_task_runtime_by_owner("current")[0].runtime_state_json
        )
        .tasks[0]
        .task_id
        == "current_work"
    )


def test_preview_cannot_authorize_retirement_after_snapshot_becomes_valid(seeded_store):
    settings, repo = seeded_store
    seed_old_creation_result(settings, repo)
    assert "old_result" in {
        row["owner_agent_id"]
        for row in maintenance.inspect_runtime_snapshots(settings, team_id="nova")
    }
    repo.save_task_runtime(
        owner_agent_id="old_result",
        owner_agent_name="Lead",
        team_id="nova",
        owner_named_agent_type="TeamArchitect",
        runtime_state_json=snapshot_json("new_work"),
    )
    before = raw_rows(settings, "active_task_runtime")
    with pytest.raises(ValueError, match="unsupported snapshot"):
        maintenance.retire_runtime_snapshots(
            settings,
            team_id="nova",
            owner_agent_ids=["old_result"],
            reason="Stale preview",
        )
    assert raw_rows(settings, "active_task_runtime") == before


@pytest.mark.parametrize(
    "corruption", ["invalid_json", "missing_task", "unknown_task_field"]
)
def test_preflight_validates_child_records_not_just_storage_header(
    seeded_store, corruption
):
    settings, _ = seeded_store
    with maintenance._connection(settings) as conn:
        bind = conn.dialect.bind
        if corruption == "missing_task":
            conn.execute(
                f"DELETE FROM active_task_runtime_records WHERE owner_agent_id={bind}",
                ("current",),
            )
        else:
            payload = (
                "invalid-json-private-task"
                if corruption == "invalid_json"
                else json.dumps(
                    {
                        "task_id": "current_work",
                        "obsolete_private_field": "private-task",
                    }
                )
            )
            conn.execute(
                f"UPDATE active_task_runtime_records SET payload_json={bind} WHERE owner_agent_id={bind}",
                (payload, "current"),
            )
    preview = maintenance.inspect_runtime_snapshots(settings, team_id="nova")
    assert "current" in {row["owner_agent_id"] for row in preview}
    assert "private-task" not in json.dumps(preview)


def test_exact_startup_failure_and_retirement_preserves_other_work(
    seeded_store, caplog
):
    settings, repo = seeded_store
    with pytest.raises(ValueError, match="Unsupported active task storage layout"):
        repo.peek_task_runtime_by_role("nova", "TeamArchitect")
    old = {
        row["owner_agent_id"]: row for row in raw_rows(settings, "active_task_runtime")
    }
    current = repo.load_task_runtime_by_owner("current")
    deferred = raw_rows(settings, "agent_deferred_messages")
    cancellations = raw_rows(settings, "runtime_task_cancellations")
    inspected = maintenance.inspect_runtime_snapshots(settings, team_id="nova")
    assert [row["owner_agent_id"] for row in inspected] == ["old_a", "old_b"]
    assert all("runtime_state_json" not in row for row in inspected)
    assert raw_rows(settings, "active_task_runtime") == list(old.values())
    ids = maintenance.retire_runtime_snapshots(
        settings,
        team_id="nova",
        owner_agent_ids=["old_a", "old_b"],
        reason="Upgrade after draining",
    )
    archived = raw_rows(settings, maintenance.ARCHIVE_TABLE)
    assert {row["archive_id"] for row in archived} == set(ids)
    for row in archived:
        original = old[row["owner_agent_id"]]
        assert all(row[key] == value for key, value in original.items())
        assert row["reason"] == "Upgrade after draining"
        if row["owner_agent_id"] == "old_a":
            assert (
                json.loads(row["records_json"])[0]["payload_json"]
                == '{"private":"payload"}'
            )
    assert maintenance.inspect_runtime_snapshots(settings, team_id="nova") == []
    recovered = repo.peek_task_runtime_by_role("nova", "TeamArchitect")
    assert recovered == current
    repo.commit_task_runtime_recovery(
        sources=recovered,
        replacement=replace(recovered[0], owner_agent_id="replacement"),
    )
    assert (
        repo.load_task_runtime_by_owner("replacement")[0].runtime_state_json
        == current[0].runtime_state_json
    )
    assert (
        next(
            row
            for row in raw_rows(settings, "active_task_runtime")
            if row["owner_agent_id"] == "other_team"
        )
        == old["other_team"]
    )
    assert raw_rows(settings, "agent_deferred_messages") == deferred
    assert raw_rows(settings, "runtime_task_cancellations") == cancellations
    assert "event=runtime_snapshots_retired" in caplog.text
    assert "private" not in caplog.text


@pytest.mark.parametrize(
    "owners", [["old_a", "current"], ["old_a", "other_team"], ["missing"], []]
)
def test_invalid_selection_cannot_retire_any_owner(seeded_store, owners):
    settings, _ = seeded_store
    before = raw_rows(settings, "active_task_runtime")
    with pytest.raises(ValueError):
        maintenance.retire_runtime_snapshots(
            settings, team_id="nova", owner_agent_ids=owners, reason="Upgrade"
        )
    assert raw_rows(settings, "active_task_runtime") == before


def test_failure_after_first_removal_rolls_back_whole_retirement(
    seeded_store, monkeypatch
):
    settings, _ = seeded_store
    before = raw_rows(settings, "active_task_runtime")
    records = raw_rows(settings, "active_task_runtime_records")
    archive = maintenance._archive_row

    def fail_second(conn, row, **kwargs):
        if row["owner_agent_id"] == "old_b":
            raise RuntimeError("Injected archive failure")
        return archive(conn, row, **kwargs)

    monkeypatch.setattr(maintenance, "_archive_row", fail_second)
    with pytest.raises(RuntimeError, match="Injected archive failure"):
        maintenance.retire_runtime_snapshots(
            settings,
            team_id="nova",
            owner_agent_ids=["old_a", "old_b"],
            reason="Upgrade",
        )
    assert raw_rows(settings, "active_task_runtime") == before
    assert raw_rows(settings, "active_task_runtime_records") == records
    with maintenance._connection(settings) as conn:
        assert not maintenance._has_table(
            conn, maintenance.ARCHIVE_TABLE, postgres=settings.backend == "postgres"
        )


def test_upgrade_before_record_table_bootstrap(seeded_store):
    settings, _ = seeded_store
    with maintenance._connection(settings) as conn:
        conn.execute("DROP TABLE active_task_runtime_records")
    maintenance.retire_runtime_snapshots(
        settings, team_id="nova", owner_agent_ids=["old_a"], reason="Upgrade"
    )
    archived = raw_rows(settings, maintenance.ARCHIVE_TABLE)
    assert archived[0]["records_json"] == "[]"
    assert archived[0]["runtime_state_json"] == snapshot_json("old_a")


def run_cli(*args):
    return subprocess.run(
        [sys.executable, "scripts/retire_runtime_snapshots.py", *args],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
    )


def test_cli_preview_confirmation_and_actual_retirement(seeded_store, tmp_path):
    settings, repo = seeded_store
    seed_old_creation_result(settings, repo)
    if settings.backend == "sqlite":
        args = ["--sqlite-path", settings.sqlite_path]
    else:
        import yaml
        from config_loader import CONFIG_SCHEMA_VERSION

        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            yaml.safe_dump(
                {
                    "schema_version": CONFIG_SCHEMA_VERSION,
                    "storage": {"backend": "postgres"},
                }
            )
        )
        secret_path = tmp_path / "dsn"
        secret_path.write_text(settings.postgres_dsn)
        env_path = tmp_path / "team.env"
        env_path.write_text(f"POSTGRES_DSN_FILE={secret_path}\n")
        args = ["--config", str(config_path), "--env", str(env_path)]
    args += ["--team-id", "nova"]
    before = raw_rows(settings, "active_task_runtime")
    preview = run_cli(*args, "--dry-run")
    assert preview.returncode == 0, preview.stderr
    assert "old_a" in preview.stderr and "current" not in preview.stderr
    assert "old_result" in preview.stderr
    assert "private-creation-result" not in preview.stderr
    assert "runtime_state_json" not in preview.stderr
    refused = run_cli(*args, "--yes")
    assert refused.returncode != 0
    assert raw_rows(settings, "active_task_runtime") == before
    completed = run_cli(
        *args, "--yes", "--owner-agent-id", "old_a", "--reason", "Upgrade"
    )
    assert completed.returncode == 0, completed.stderr
    assert "Archived 1 snapshot(s)" in completed.stderr


def test_cli_resolves_generic_and_instance_configs(tmp_path):
    import yaml
    from assistant_gateway.config import GatewayInstanceConfig
    from config_loader import CONFIG_SCHEMA_VERSION

    generic_path = tmp_path / "config.yaml"
    db_path = tmp_path / "runtime.db"
    generic_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": CONFIG_SCHEMA_VERSION,
                "agent": {"instance_root": str(tmp_path)},
                "storage": {"backend": "sqlite", "sqlite": {"path": str(db_path)}},
            }
        )
    )
    instance = GatewayInstanceConfig.create(
        instance_name="snapshot_repair",
        instance_root=str(tmp_path / "instance"),
        team_id="nova",
        timezone="UTC",
        heartbeat_enabled=False,
        manager_storage_backend="sqlite",
        manager_storage_sqlite_path=str(db_path),
    )
    instance_path = instance.save(tmp_path / "instance" / "instance_config.yaml")
    settings = resolve_storage_settings(
        {"backend": "sqlite", "sqlite": {"path": str(db_path)}}
    )
    create_runtime_state_repository(settings)
    for config in (generic_path, instance_path):
        result = run_cli("--config", str(config), "--team-id", "nova", "--dry-run")
        assert result.returncode == 0, result.stderr


def test_missing_database_is_not_created(tmp_path):
    missing = tmp_path / "missing.db"
    result = run_cli("--sqlite-path", str(missing), "--team-id", "nova", "--dry-run")
    assert result.returncode != 0
    assert not missing.exists()
