"""Saved scheduled-worker settings in the old access format (Phase 4c-4 cutover).

Binding schema version 1 kept the removed permission system's policy and
request ids and the authority mode ``authorized``. The repository refuses such
rows with a message naming ``assistant_cli access approve-existing``; startup
refuses while any remain; the command rewrites them in place (real SQLite).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from assistant_gateway.config import GatewayInstanceConfig
from custom_messages.contracts.schedule_worker_v1 import (
    ScheduleWorkerBindingV1,
    ScheduleWorkerPolicySpecV1,
)
from docker.launch_headless import RuntimeLayoutError, validate_runtime_layout
from runtime_settings.approval_cutover import old_approval_message, old_approval_records, reapprove
from storage.schedule_worker_repository import (
    OldScheduleWorkerBinding,
    ScheduleWorkerRepository,
    old_schedule_worker_bindings,
)

TEAM = "team_cutover"
APPROVE_EXISTING = "access approve-existing"


def _config(tmp_path: Path) -> GatewayInstanceConfig:
    root = tmp_path / "instance"
    config = GatewayInstanceConfig.create(
        instance_name="cutover",
        instance_root=str(root),
        team_id=TEAM,
        timezone="UTC",
        manager_storage_sqlite_path="runtime/task_history.db",
    )
    config.save(root / "instance_config.yaml")
    return config


def _old_payload(schedule_id: str, *, authority_mode: str = "authorized") -> str:
    current = ScheduleWorkerBindingV1(
        schedule_id=schedule_id,
        team_id=TEAM,
        owner_canonical_user_id="user_dana",
        target_agent_type="Worker",
        definition_digest="d" * 64,
        execution_digest="e" * 64,
        revision=3,
        policy=ScheduleWorkerPolicySpecV1(),
        actor_principal_id="agent_lead",
        authority_mode="held_access",
    ).model_dump(mode="json")
    current.pop("schema_version")
    return json.dumps(
        {
            **current,
            "authority_mode": authority_mode,
            "observed_policy_ids": ["policy_old"],
            "access_review_request_ids": ["perm_request_old"],
        }
    )


def _insert(config: GatewayInstanceConfig, schedule_id: str, payload: str, previous: str | None = None) -> None:
    ScheduleWorkerRepository(config.manager_storage_settings)  # creates the tables
    with sqlite3.connect(str(config.manager_storage_settings.sqlite_path)) as conn:
        conn.execute(
            "INSERT INTO schedule_worker_bindings(schedule_id,team_id,owner_id,revision,payload_json,previous_binding_json) "
            "VALUES (?,?,?,?,?,?)",
            (schedule_id, TEAM, "user_dana", 3, payload, previous),
        )


def test_old_binding_rows_are_refused_until_approve_existing_rewrites_them(tmp_path: Path) -> None:
    config = _config(tmp_path)
    settings = config.manager_storage_settings
    _insert(config, "sched_old", _old_payload("sched_old"))
    _insert(config, "sched_pending", _old_payload("sched_pending", authority_mode="agreement"), previous=_old_payload("sched_pending"))
    repository = ScheduleWorkerRepository(settings)

    with pytest.raises(OldScheduleWorkerBinding, match=APPROVE_EXISTING):
        repository.get_binding("sched_old", team_id=TEAM)
    with pytest.raises(OldScheduleWorkerBinding):
        repository.list_bindings(TEAM)

    found = old_approval_records(config.instance_root_path, storage_settings=settings)
    assert found.present
    assert found.schedule_worker_bindings == ["sched_old", "sched_pending"]
    message = old_approval_message(found, config_path=tmp_path / "instance" / "instance_config.yaml")
    assert "scheduled-worker settings of 2 schedule(s)" in message and APPROVE_EXISTING in message

    changed = reapprove(found, person_id="user_admin", now=datetime.now(timezone.utc))

    assert changed == [
        "scheduled-worker settings of schedule sched_old",
        "scheduled-worker settings of schedule sched_pending",
    ]
    assert old_schedule_worker_bindings(settings) == ()
    rewritten = repository.get_binding("sched_old", team_id=TEAM)
    assert rewritten is not None
    assert rewritten.schema_version == 2
    assert rewritten.authority_mode == "held_access"
    assert rewritten.revision == 3 and rewritten.target_agent_type == "Worker"
    assert repository.get_binding("sched_pending", team_id=TEAM).authority_mode == "agreement"
    # No schedule change is pending, so the saved binding is also the active one.
    assert repository.get_binding("sched_pending", team_id=TEAM, active=True).authority_mode == "agreement"
    with sqlite3.connect(str(settings.sqlite_path)) as conn:
        stored_previous = conn.execute(
            "SELECT previous_binding_json FROM schedule_worker_bindings WHERE schedule_id='sched_pending'"
        ).fetchone()[0]
    assert json.loads(stored_previous)["authority_mode"] == "held_access"
    assert "observed_policy_ids" not in json.loads(stored_previous)


def test_startup_refuses_while_old_binding_rows_remain(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _insert(config, "sched_old", _old_payload("sched_old"))

    with pytest.raises(RuntimeLayoutError) as refused:
        validate_runtime_layout(config)

    assert "sched_old" in str(refused.value) and APPROVE_EXISTING in str(refused.value)
