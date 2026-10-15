import logging
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
from tests.runtime_snapshot_fixtures import snapshot_json

from tests.postgres_test_helpers import patch_fake_postgres
from storage.config import resolve_storage_settings
from storage.runtime_state_repository import create_runtime_state_repository


def test_runtime_task_cancellation_is_durable_and_team_scoped(
    tmp_path: Path,
) -> None:
    settings = resolve_storage_settings(
        {"backend": "sqlite", "sqlite": {"path": "runtime/task_history.db"}},
        base_dir=str(tmp_path),
    )
    repo = create_runtime_state_repository(settings, logger=logging.getLogger("test"))
    cancelled_at = datetime.now(timezone.utc).isoformat()

    repo.record_task_cancellations(
        team_id="team_1",
        root_task_id="task_root",
        task_ids=["task_root", "task_child", "task_child"],
        canonical_user_id="user_1",
        command_id="runtime_work_command_1",
        reason="Operator stop.",
        cancelled_at=cancelled_at,
    )

    child = repo.get_task_cancellation(team_id="team_1", task_id="task_child")
    assert child is not None
    assert child.root_task_id == "task_root"
    assert child.canonical_user_id == "user_1"
    assert child.command_id == "runtime_work_command_1"
    assert child.cancelled_at == cancelled_at
    assert repo.get_task_cancellation(team_id="team_2", task_id="task_child") is None


def test_runtime_state_repository_claims_deferred_rows_by_owner(tmp_path: Path) -> None:
    settings = resolve_storage_settings(
        {"backend": "sqlite", "sqlite": {"path": "runtime/task_history.db"}},
        base_dir=str(tmp_path),
    )
    repo = create_runtime_state_repository(settings, logger=logging.getLogger("test"))

    repo.save_deferred_messages(
        owner_agent_id="agent_a",
        owner_agent_name="Agent A",
        team_id="team_1",
        owner_named_agent_type="CodeWritingAgent",
        messages_json='["m1"]',
    )

    assert repo.get_deferred_messages_team_id("agent_a") == "team_1"
    rows = repo.load_deferred_messages_by_owner("agent_a")
    assert len(rows) == 1
    assert repo.load_deferred_messages_by_owner("agent_a") == rows
    repo.commit_deferred_messages_recovery(
        sources=rows, replacement=replace(rows[0], owner_agent_id="replacement"),
    )
    assert repo.load_deferred_messages_by_owner("agent_a") == []
    assert repo.load_deferred_messages_by_owner("replacement")[0].messages_json == '["m1"]'


@pytest.mark.parametrize("state_kind", ["task_runtime", "deferred_messages"])
def test_recovery_commit_failure_preserves_the_original_snapshot(tmp_path, monkeypatch, state_kind):
    settings = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "runtime.db")}})
    repo = create_runtime_state_repository(settings)
    payload_field = "runtime_state_json" if state_kind == "task_runtime" else "messages_json"
    getattr(repo, f"save_{state_kind}")(
        owner_agent_id="original", owner_agent_name="Original", team_id="team", owner_named_agent_type="Worker",
        **{payload_field: snapshot_json() if state_kind == "task_runtime" else '{"work":1}'},
    )
    load = getattr(repo, f"load_{state_kind}_by_owner")
    sources = load("original")
    replacement = replace(sources[0], owner_agent_id="replacement")
    original_delete = repo._delete_rows_with_connection

    def fail_after_delete(*args):
        original_delete(*args)
        raise RuntimeError("crash before replacement insert")

    with monkeypatch.context() as patcher:
        patcher.setattr(repo, "_delete_rows_with_connection", fail_after_delete)
        with pytest.raises(RuntimeError, match="crash before"):
            getattr(repo, f"commit_{state_kind}_recovery")(sources=sources, replacement=replacement)
    assert load("original") == sources
    assert load("replacement") == []
    getattr(repo, f"commit_{state_kind}_recovery")(sources=sources, replacement=replacement)
    assert load("original") == []
    assert len(load("replacement")) == 1


def test_runtime_state_repository_accepts_postgres_when_driver_is_available(tmp_path: Path, monkeypatch) -> None:
    settings = resolve_storage_settings(
        {
            "backend": "postgres",
            "postgres": {"dsn": "postgresql://team:secret@db/team_agents"},
        },
        base_dir=str(tmp_path),
    )
    patch_fake_postgres(monkeypatch)

    repo = create_runtime_state_repository(settings, logger=logging.getLogger("test"))
    assert repo is not None
