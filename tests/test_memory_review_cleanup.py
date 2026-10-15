"""Real-database retirement, archive, retention and stale-worker protection."""

# ruff: noqa: F811 -- imported pytest fixture

from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from custom_agents.memory_review_store import MemoryReviewStore
from custom_messages.contracts.common_v1 import contract_sha256
from storage.memory_review_cleanup_repository import (
    MemoryReviewCleanupRepository,
    EVENT_TABLE,
)
from storage.runtime_snapshot_maintenance import _connection
from storage.runtime_state_repository import create_runtime_state_repository
from tests.runtime_snapshot_fixtures import snapshot_json
from tests.test_schedule_worker_service import lead  # noqa: F401


NOW = datetime(2026, 9, 19, tzinfo=timezone.utc)


def seed(lead, *, owner="old-worker", team="team", expiry="2026-06-21T03:40:19+00:00"):
    store = MemoryReviewStore(storage_settings=lead.task_manager.storage_settings)
    job = store.enqueue_review_job(
        owner_team_id=team,
        owner_named_agent_type="MemReviewAgent",
        task_id="old-source",
        conversation_id="old-thread",
        turn_index=1,
        source_payload={"review_kind": "coremem_audit", "facts": ["PRIVATE_FACT"]},
    )
    store.claim_review_job(
        job.review_job_id,
        owner_team_id=team,
        owner_named_agent_type="MemReviewAgent",
        claimer_agent_id=owner,
    )
    with _connection(lead.task_manager.storage_settings) as conn:
        conn.execute(
            "UPDATE memory_review_jobs SET claim_expires_at=? WHERE review_job_id=?",
            (expiry, job.review_job_id),
        )
    return (
        store,
        job.review_job_id,
        MemoryReviewCleanupRepository(lead.task_manager.storage_settings),
    )


def retire(repository):
    return repository.retire_expired(
        "team", before=NOW, now=NOW, actor_id="runtime", limit=100
    )


def test_retirement_preserves_content_and_does_not_replay_or_complete(lead):
    store, job_id, repo = seed(lead)
    before = store.get_review_job(job_id)
    assert retire(repo) == [job_id]
    after = store.get_review_job(job_id)
    assert after.status == "expired"
    assert after.source_payload_json == before.source_payload_json
    assert after.claim_owner_agent_id == before.claim_owner_agent_id
    assert after.claim_expires_at == before.claim_expires_at
    assert store.list_review_jobs(pending_only=True) == []
    assert (
        store.claim_review_job(
            job_id,
            owner_team_id="team",
            owner_named_agent_type="MemReviewAgent",
            claimer_agent_id="new-worker",
        )
        is None
    )
    with pytest.raises(ValueError, match="not claimed"):
        store.mark_review_job_completed(job_id, claimer_agent_id="old-worker")
    assert retire(repo) == []
    with _connection(lead.task_manager.storage_settings) as conn:
        assert conn.execute(f"SELECT COUNT(*) FROM {EVENT_TABLE}").fetchone()[0] == 1
    assert "PRIVATE_FACT" not in json.dumps(repo.inspect("team"))


@pytest.mark.parametrize(
    "protection",
    [
        "unexpired",
        "grace",
        "unknown_date",
        "live_worker",
        "saved_task",
        "unsupported",
        "other_team",
    ],
)
def test_retirement_preserves_work_that_is_not_proven_abandoned(lead, protection):
    store, job_id, repo = seed(
        lead, team="other" if protection == "other_team" else "team"
    )
    before = NOW
    workers = ()
    if protection in {"unexpired", "unknown_date"}:
        date = (
            (NOW + timedelta(minutes=5)).isoformat()
            if protection == "unexpired"
            else "invalid"
        )
        with _connection(lead.task_manager.storage_settings) as conn:
            conn.execute(
                "UPDATE memory_review_jobs SET claim_expires_at=? WHERE review_job_id=?",
                (date, job_id),
            )
    elif protection == "grace":
        before = datetime(2026, 6, 20, tzinfo=timezone.utc)
    elif protection == "live_worker":
        workers = ("old-worker",)
    elif protection in {"saved_task", "unsupported"}:
        create_runtime_state_repository(
            lead.task_manager.storage_settings
        ).save_task_runtime(
            owner_agent_id="old-worker",
            owner_agent_name="Old worker",
            team_id="team",
            owner_named_agent_type="MemReviewAgent",
            runtime_state_json=snapshot_json("unfinished"),
        )
        if protection == "unsupported":
            with _connection(lead.task_manager.storage_settings) as conn:
                conn.execute(
                    "UPDATE active_task_runtime SET runtime_state_json='unsupported' WHERE owner_agent_id='old-worker'"
                )
                conn.execute(
                    "DELETE FROM active_task_runtime_records WHERE owner_agent_id='old-worker'"
                )
    assert (
        repo.retire_expired(
            "team",
            before=before,
            now=NOW,
            actor_id="runtime",
            limit=100,
            protected_workers=workers,
        )
        == []
    )
    assert store.get_review_job(job_id).status == "claimed"


def test_archival_and_later_purge_retain_audit_and_enforce_retention(lead):
    store, job_id, repo = seed(lead)
    retire(repo)

    def maintain(operation, when):
        return repo.maintain(
            "team",
            [job_id],
            operation=operation,
            expected_digest=contract_sha256({"jobs": repo.selected("team", [job_id])}),
            actor_id="maintenance",
            reason="Old review; outcome unknown",
            now=when,
            retention_days=90,
        )

    assert maintain("archive", NOW) == [job_id]
    assert store.get_review_job(job_id).status == "archived"
    assert store.get_review_job(job_id).source_payload["facts"] == ["PRIVATE_FACT"]
    with pytest.raises(ValueError, match="retention"):
        maintain("purge", NOW + timedelta(days=89))
    assert maintain("purge", NOW + timedelta(days=90)) == [job_id]
    assert store.get_review_job(job_id) is None
    with _connection(lead.task_manager.storage_settings) as conn:
        assert [
            row[0]
            for row in conn.execute(
                f"SELECT to_status FROM {EVENT_TABLE} ORDER BY created_at, CASE to_status WHEN 'expired' THEN 0 WHEN 'archived' THEN 1 ELSE 2 END"
            ).fetchall()
        ] == ["expired", "archived", "purged"]


def test_changed_claim_cannot_be_retired_from_old_preview(lead):
    store, job_id, repo = seed(lead)
    with _connection(lead.task_manager.storage_settings) as conn:
        conn.execute(
            "UPDATE memory_review_jobs SET claim_expires_at=? WHERE review_job_id=?",
            ((NOW + timedelta(days=1)).isoformat(), job_id),
        )
    with pytest.raises(ValueError, match="changed since preview"):
        repo.retire_expired(
            "team",
            before=NOW,
            now=NOW,
            actor_id="operator",
            limit=1,
            expected_ids={job_id},
        )
    assert store.get_review_job(job_id).status == "claimed"


def test_archive_checks_exact_state_and_preserves_entire_batch_on_error(lead):
    store, job_id, repo = seed(lead)
    _, active, _ = seed(lead, expiry=(NOW + timedelta(days=1)).isoformat())
    retire(repo)
    ids = [job_id, active]
    with pytest.raises(ValueError, match="Only expired"):
        repo.maintain(
            "team",
            ids,
            operation="archive",
            expected_digest=contract_sha256({"jobs": repo.selected("team", ids)}),
            actor_id="agent",
            reason="Review",
            now=NOW,
            retention_days=90,
        )
    assert store.get_review_job(job_id).status == "expired"
    with pytest.raises(ValueError, match="state changed"):
        repo.maintain(
            "team",
            [job_id],
            operation="archive",
            expected_digest="stale",
            actor_id="agent",
            reason="Review",
            now=NOW,
            retention_days=90,
        )
    with pytest.raises(ValueError, match="this team"):
        repo.selected("other", [job_id])


def test_scan_installs_internal_maintenance_once_and_preserves_disabled_timer(lead):
    from custom_agents.memory_review_cleanup_runtime import scan_review_cleanup
    from custom_agents.internal_improvement_runtime import (
        internal_improvement_schedule_id,
    )

    store, job_id, _ = seed(lead)
    assert scan_review_cleanup(lead, ())["retired"] == [job_id]
    schedule_id = internal_improvement_schedule_id("team")
    timer = lead._schedule_store.get_scheduled_message(schedule_id)
    assert timer is not None
    assert timer.claim_named_agent_type == "TeamArchitect"
    assert timer.payload_meta.to_named_agent_type == "InternalImprovementAgent"
    # Use the underlying fixture connection to seed a previously disabled timer.
    with _connection(lead.task_manager.storage_settings) as conn:
        conn.execute(
            "UPDATE scheduled_messages SET enabled=0 WHERE schedule_id=?",
            (schedule_id,),
        )
    scan_review_cleanup(lead, ())
    rows = [
        r for r in lead._schedule_store.get_all() if r["schedule_id"] == schedule_id
    ]
    assert len(rows) == 1 and not rows[0]["enabled"]
    assert store.get_review_job(job_id).status == "expired"


def test_cleanup_service_applies_the_checked_batch_as_the_calling_agent(lead):
    """Who may clean up is decided per call by the access hook; the service keeps its batch rules."""

    import sqlite3

    from custom_agents.memory_review_cleanup_service import MemoryReviewCleanupService
    from storage.memory_review_cleanup_repository import EVENT_TABLE

    store, job_id, repo = seed(lead)
    retire(repo)
    service = MemoryReviewCleanupService(lead)

    with pytest.raises(ValueError, match="brief cleanup reason"):
        service.maintain([job_id], "archive", "", "request")
    response = service.maintain([job_id], "archive", "Keep history", "request")

    assert response["status"] == "ok" and response["review_job_ids"] == [job_id]
    assert store.get_review_job(job_id).status == "archived"
    with sqlite3.connect(lead.task_manager.storage_settings.sqlite_path) as conn:
        actors = {row[0] for row in conn.execute(f"SELECT actor_id FROM {EVENT_TABLE} WHERE to_status = 'archived'")}
    assert actors == {lead.id}



def test_cleanup_tools_are_system_duties_that_other_agents_need_a_grant_for():
    """Only system agents (MemReviewAgent, InternalImprovementAgent) hold them by default; purging needs full access.

    The review queue is also the team lead's and members' (like team and
    personal memory), because it shows only the task person's own jobs.
    """

    from access_catalog.catalog import load_catalog
    from custom_messages.contracts.access_v2 import AccessLevel

    catalog = load_catalog(None)
    maintenance = catalog.tools["fn:memory_review_maintenance"]

    assert tuple(maintenance.tool_sets) == ("system_duties",)
    assert tuple(catalog.tools["fn:memory_review_inspection"].tool_sets) == ("team_lead_runtime", "system_duties")
    purge = catalog.resolve_function("maintain_memory_review_jobs", {"operation": "purge"})
    archive = catalog.resolve_function("maintain_memory_review_jobs", {"operation": "archive"})
    assert (purge.level, archive.level) == (AccessLevel.FULL, AccessLevel.WRITE)



@pytest.mark.asyncio
async def test_maintenance_can_start_worker_without_current_chat_task(lead):
    from custom_agents.internal_improvement_runtime import (
        build_internal_improvement_schedule,
        prepare_internal_improvement_schedule_for_manager,
        ensure_maintenance_worker,
    )
    from custom_messages.enums import ProgressState

    lead.task_manager.update_main_task_state(
        "request", ProgressState.COMPLETED, NOW.isoformat()
    )
    assert lead.task_manager.current_task_id is None
    lead.bootstrap_config = {"canonical_user_id": "schedule-owner"}
    lead.definitions["InternalImprovementAgent"] = {
        "named_agent_type": "InternalImprovementAgent",
        "role": "maintenance",
        "goal": "Review cleanup",
        "tools": [],
    }
    lead._probe_live_named_agent_type.side_effect = [
        None,
        SimpleNamespace(self_id="new-worker"),
    ]
    schedule = prepare_internal_improvement_schedule_for_manager(
        lead, build_internal_improvement_schedule(lead)
    )
    assert await ensure_maintenance_worker(lead, schedule, "due-now")
    assert lead.starts[0].owner_canonical_user_id == "schedule-owner"
    assert lead.starts[0].named_agent_type == "InternalImprovementAgent"


@pytest.mark.parametrize("new_blocker", [False, True])
def test_offline_upgrade_retirement_is_explicit_backs_up_content_and_keeps_queued_jobs(
    lead, tmp_path, monkeypatch, caplog, new_blocker
):
    import sqlite3
    import yaml
    from config_loader import CONFIG_SCHEMA_VERSION
    from scripts.upgrade_local_schedules import main
    from tests.test_local_schedule_runtime import memory_timer

    memory_timer(lead)
    store, job_id, _ = seed(lead)
    queued = store.enqueue_review_job(
        owner_team_id="team",
        owner_named_agent_type="MemReviewAgent",
        task_id="queued",
        conversation_id="queued",
        turn_index=0,
        source_payload={"review_kind": "coremem_audit"},
    )
    if new_blocker:
        seed(lead, expiry="2999-01-01T00:00:00+00:00")
    config = tmp_path / "upgrade.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "schema_version": CONFIG_SCHEMA_VERSION,
                "storage": {
                    "backend": "sqlite",
                    "sqlite": {"path": lead.task_manager.storage_settings.sqlite_path},
                },
                "agent": {"instance_root": str(tmp_path)},
            }
        )
    )
    base = ["upgrade_local_schedules", "--config", str(config), "--team-id", "team"]
    monkeypatch.setattr("sys.argv", base)
    assert main() == 1
    base += ["--retire-expired-review-claims"]
    with sqlite3.connect(lead.task_manager.storage_settings.sqlite_path) as conn:
        before = "\n".join(conn.iterdump())
    monkeypatch.setattr("sys.argv", base)
    assert main() == int(new_blocker)
    with sqlite3.connect(lead.task_manager.storage_settings.sqlite_path) as conn:
        assert "\n".join(conn.iterdump()) == before
    backup = tmp_path / "backup.json"
    monkeypatch.setattr(
        "sys.argv", base + ["--apply", "--runtime-stopped", "--backup", str(backup)]
    )
    assert main() == int(new_blocker)
    assert "PRIVATE_FACT" not in caplog.text
    assert store.get_review_job(queued.review_job_id) == queued
    if new_blocker:
        assert not backup.exists()
        assert store.get_review_job(job_id).status == "claimed"
        return
    saved = json.loads(backup.read_text())
    assert saved["memory_review_jobs"][0]["status"] == "claimed"
    assert "PRIVATE_FACT" in saved["memory_review_jobs"][0]["source_payload_json"]
    assert store.get_review_job(job_id).status == "expired"
    assert (
        lead._schedule_store.get_scheduled_message("job").claim_named_agent_type
        == "MemReviewAgent"
    )
    monkeypatch.setattr("sys.argv", base)
    assert main() == 0


def test_cleanup_settings_are_validated_for_generic_and_instance_configuration():
    from runtime_settings.agents import AgentRuntimeDefaultsSettings
    from assistant_gateway.config import GatewayInstanceConfig
    from pydantic import ValidationError

    defaults = AgentRuntimeDefaultsSettings().schedule_maintenance[
        "memory_review_cleanup"
    ]
    assert defaults["enabled"] and defaults["archive_retention_days"] == 90
    assert (
        GatewayInstanceConfig(
            instance_name="test", instance_root="/tmp/test"
        ).agent.schedule_maintenance["memory_review_cleanup"]
        == defaults
    )
    with pytest.raises(ValidationError):
        AgentRuntimeDefaultsSettings(
            schedule_maintenance={
                "memory_review_cleanup": {"archive_retention_days": 0}
            }
        )


@pytest.mark.parametrize("state", ["reserved", "starting", "ready", "unknown"])
def test_other_host_or_starting_worker_is_protected_without_a_task_snapshot(
    lead, state
):
    from storage.agent_launch_repository import AgentLaunchRepository

    store, job_id, repo = seed(lead)
    AgentLaunchRepository(lead.task_manager.storage_settings)
    with _connection(lead.task_manager.storage_settings) as conn:
        conn.execute(
            "INSERT INTO agent_launches VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "old-launch",
                "old-worker",
                "team",
                "MemReviewAgent",
                state,
                1,
                NOW.isoformat(),
                NOW.isoformat(),
                "{}",
            ),
        )
    assert retire(repo) == []
    assert store.get_review_job(job_id).status == "claimed"


def test_known_remote_review_worker_is_protected_from_online_cleanup(lead):
    from custom_agents.memory_review_cleanup_runtime import (
        protected_live_worker_ids,
        scan_review_cleanup,
    )

    store, job_id, _ = seed(lead)
    lead.known_agents = SimpleNamespace(
        agent_map={
            "old-worker": SimpleNamespace(
                self_id="old-worker", team_id="team", is_exiting=False
            )
        }
    )
    assert scan_review_cleanup(lead, protected_live_worker_ids(lead))["retired"] == []
    assert store.get_review_job(job_id).status == "claimed"


def test_disabled_cleanup_scan_does_not_modify_jobs_or_install_maintenance(lead):
    from custom_agents.memory_review_cleanup_runtime import scan_review_cleanup
    from custom_agents.internal_improvement_runtime import (
        internal_improvement_schedule_id,
    )

    store, job_id, _ = seed(lead)
    lead.schedule_maintenance = {"memory_review_cleanup": {"enabled": False}}
    assert scan_review_cleanup(lead, ()) == {
        "retired": [],
        "maintenance_pending": False,
    }
    assert store.get_review_job(job_id).status == "claimed"
    assert (
        lead._schedule_store.get_scheduled_message(
            internal_improvement_schedule_id("team")
        )
        is None
    )
