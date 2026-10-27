"""Local catalog defaults, explicit upgrade and worker selection using real stores."""
# ruff: noqa: F811 -- imported pytest fixture is injected by name

from types import SimpleNamespace

import pytest

from config_loader import get_config
from custom_agents.local_schedule_policy import install_schedule_default
from custom_agents.local_schedule_upgrade import (
    apply_local_schedule_upgrade,
    plan_local_schedule_upgrade,
)
from custom_agents.schedule_worker_controller import ScheduleWorkerController
from custom_messages.enums import ScheduleManagementClass, SystemScheduleKind
from tests.system_schedule_kind_support import (
    CURATOR_AGENT_TYPE,
    PERSON_KIND,
    STAND_IN_KIND,
    use_kind,
)
from tests.test_schedule_worker_service import (  # noqa: F401
    configure,
    lead,
    worker_definition,
)
from tests.tool_result_support import tool_json


def ship(lead, name: str = "Worker") -> None:
    """Make an agent type one that ships with the code."""
    lead.definitions[name] = worker_definition(name, source="release")


def memory_timer(lead):
    ship(lead, "MemReviewAgent")
    schedule = lead._schedule_store.get_scheduled_message("job")
    schedule.enabled = True
    schedule.management_class = ScheduleManagementClass.SYSTEM
    schedule.system_schedule_kind = SystemScheduleKind.MEMORY_REVIEW_COREMEM
    schedule.schedule_name = "memory_review:coremem:instance"
    schedule.claim_named_agent_type = "TeamArchitect"
    lead._schedule_store.add(schedule)
    return schedule


def curator_timer(lead, *, claim: str = CURATOR_AGENT_TYPE):
    """The owner's timer of a new per-person kind (tests/system_schedule_kind_support.py)."""
    ship(lead, CURATOR_AGENT_TYPE)
    schedule = lead._schedule_store.get_scheduled_message("job")
    schedule.enabled = True
    schedule.management_class = ScheduleManagementClass.SYSTEM
    schedule.system_schedule_kind = STAND_IN_KIND
    schedule.schedule_name = "knowledge_review:person:instance:owner"
    schedule.claim_named_agent_type = claim
    lead._schedule_store.add(schedule)
    return schedule


def test_a_new_per_person_kind_runs_on_its_own_on_demand_worker(lead, tmp_path, monkeypatch):
    """A new row in the system schedule table gets the default startup policy
    for its own agent type and accepts only that type as its worker, as memory
    review does, with no code change."""


    from custom_agents.manager_control_runtime import _schedule_worker_assignment
    from custom_agents.system_schedule_changes import SystemScheduleChangeService
    from custom_tools.providers.schedule_workers import configure_scheduled_worker
    from tests.task_work_support import set_agent_ref
    from tests.change_approval_support import agreement

    use_kind(monkeypatch, PERSON_KIND)
    lead.instance_root = str(tmp_path)
    schedule = curator_timer(lead)

    assert install_schedule_default(lead, schedule)
    binding = lead._schedule_store.worker_records.get_binding("job", team_id="team")
    assert (binding.target_agent_type, binding.authority_mode) == (CURATOR_AGENT_TYPE, "runtime_default")
    assignment = _schedule_worker_assignment(lead._schedule_store, schedule, "owner")
    assert (assignment.review_agent_type, assignment.upgrade_required) == (CURATOR_AGENT_TYPE, False)

    set_agent_ref(lead)
    refused = configure_scheduled_worker("job", "Worker", binding.revision, worker_policy_json='{"enabled": true}')
    assert refused["status"] == "error"
    assert "knowledge_review_requires_KnowledgeCuratorAgent" in tool_json(refused)["error"]
    staged = tool_json(
        configure_scheduled_worker(
            "job",
            CURATOR_AGENT_TYPE,
            binding.revision,
            worker_policy_json='{"enabled": true, "rules": {"claim_wait_seconds": 5}}',
        )
    )
    assert staged["status"] == "approval_required"
    applied = SystemScheduleChangeService(lead).apply(staged["change_id"], agreement())
    assert applied["configured"] is True
    saved = lead._schedule_store.worker_records.get_binding("job", team_id="team")
    assert (saved.target_agent_type, saved.policy.rules) == (CURATOR_AGENT_TYPE, {"claim_wait_seconds": 5})


def test_a_new_per_person_kind_claimed_by_another_type_gets_no_local_worker(lead, monkeypatch):
    from custom_agents.local_schedule_policy import supports_local_worker
    from custom_agents.manager_control_runtime import _schedule_worker_assignment

    use_kind(monkeypatch, PERSON_KIND)
    schedule = curator_timer(lead, claim="TeamArchitect")

    assert not supports_local_worker(schedule)
    assert not install_schedule_default(lead, schedule)
    assignment = _schedule_worker_assignment(lead._schedule_store, schedule, "owner")
    assert (assignment.review_agent_type, assignment.upgrade_required) == (CURATOR_AGENT_TYPE, True)


@pytest.mark.asyncio
async def test_current_deny_stops_default_start(lead):
    ship(lead)
    lead._schedule_store.set_enabled("job", True)
    install_schedule_default(lead, lead._schedule_store.get_scheduled_message("job"))
    get_config().get("agent.schedule_workers")["denied_agent_types"] = ["Worker"]
    await ScheduleWorkerController(lead).reconcile()
    assert not lead.starts
    assert (
        lead._schedule_store.worker_records.get_binding(
            "job", team_id="team"
        ).blocked_reason
        == "agent_type_disallowed"
    )


def test_upgrade_refuses_live_claim_without_retargeting(lead):
    memory_timer(lead)
    assert lead._schedule_store.claim_next_due(
        claimer_agent_id="running",
        claimer_named_agent_type="TeamArchitect",
        claimer_team_id="team",
    )
    with pytest.raises(ValueError, match="active schedule claims"):
        apply_local_schedule_upgrade(lead)
    assert (
        lead._schedule_store.get_scheduled_message("job").claim_named_agent_type
        == "TeamArchitect"
    )


def test_memory_review_worker_change_is_staged_then_applied_with_agreement(lead, tmp_path, caplog):
    """The memory-review timer is managed by the runtime: re-saving its worker
    policy from a tool is an exact change applied only after approval (here
    the person's agreement, with authorization off)."""

    import logging

    from custom_agents.system_schedule_changes import SystemScheduleChangeService
    from custom_tools.providers.schedule_workers import configure_scheduled_worker
    from tests.task_work_support import set_agent_ref
    from tests.change_approval_support import agreement

    lead.instance_root = str(tmp_path)
    memory_timer(lead)
    apply_local_schedule_upgrade(lead)
    binding = lead._schedule_store.worker_records.get_binding("job", team_id="team")
    set_agent_ref(lead)

    staged = tool_json(
        configure_scheduled_worker(
            "job",
            "MemReviewAgent",
            binding.revision,
            worker_policy_json='{"enabled": true, "rules": {"claim_wait_seconds": 5}}',
        )
    )

    assert staged["status"] == "approval_required"
    assert staged["title"].startswith("Set the worker for the system schedule")
    unchanged = lead._schedule_store.worker_records.get_binding("job", team_id="team")
    assert unchanged.revision == binding.revision
    with caplog.at_level(logging.INFO, logger="custom_agents.schedule_management"):
        applied = SystemScheduleChangeService(lead).apply(staged["change_id"], agreement())

    assert applied["configured"] is True
    assert applied["authority"]["mode"] == "agreement"
    saved = lead._schedule_store.worker_records.get_binding("job", team_id="team")
    assert saved.revision == binding.revision + 1
    assert saved.policy.rules == {"claim_wait_seconds": 5}
    assert saved.authority_mode == "agreement"
    assert "authority_mode=agreement" in caplog.text
    with pytest.raises(FileExistsError):
        SystemScheduleChangeService(lead).apply(staged["change_id"], agreement())


def test_upgrade_preview_does_not_create_or_modify_tables(lead):
    import sqlite3
    from scripts.upgrade_local_schedules import read_upgrade_state

    memory_timer(lead)
    path = lead.task_manager.storage_settings.sqlite_path
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE schedule_worker_definition_records")
        before = "\n".join(conn.iterdump())
    backup, view = read_upgrade_state(lead.task_manager.storage_settings, "team")
    preview = SimpleNamespace(**{**vars(lead), "_schedule_store": view})
    assert plan_local_schedule_upgrade(preview)[0]["action"] == "upgrade"
    assert backup["schedules"][0]["claim_named_agent_type"] == "TeamArchitect"
    with sqlite3.connect(path) as conn:
        assert "\n".join(conn.iterdump()) == before


@pytest.mark.parametrize("claim_ttl", [None, 300, -1])
def test_upgrade_distinguishes_backlog_from_claimed_work_without_mutation(
    lead, claim_ttl
):
    import json
    import sqlite3
    from scripts.upgrade_local_schedules import inspect_review_work
    from custom_agents.memory_review_store import MemoryReviewStore

    store = MemoryReviewStore(storage_settings=lead.task_manager.storage_settings)
    job = store.enqueue_review_job(
        owner_team_id="team",
        owner_named_agent_type="MemReviewAgent",
        task_id="old-task",
        conversation_id="old-thread",
        turn_index=1,
        source_payload={"facts": ["PRIVATE_REVIEW_CONTENT"]},
    )
    if claim_ttl is not None:
        job = store.claim_review_job(
            job.review_job_id,
            owner_team_id="team",
            owner_named_agent_type="MemReviewAgent",
            claimer_agent_id="old-worker",
            claim_ttl_seconds=claim_ttl,
        )
    with sqlite3.connect(lead.task_manager.storage_settings.sqlite_path) as conn:
        before = "\n".join(conn.iterdump())
    report = inspect_review_work(lead.task_manager.storage_settings, "team")
    assert report["blocks_upgrade"] is (claim_ttl is not None)
    jobs = (
        report["queued_review_jobs"]
        if claim_ttl is None
        else report["claimed_review_jobs"]
    )
    assert jobs[0]["review_job_id"] == job.review_job_id
    assert jobs[0]["source_task_id"] == "old-task"
    assert jobs[0]["claim_owner_agent_id"] == job.claim_owner_agent_id
    assert jobs[0]["claim_expires_at"] == job.claim_expires_at
    assert "PRIVATE_REVIEW_CONTENT" not in json.dumps(report)
    assert store.get_review_job(job.review_job_id) == job
    other = inspect_review_work(lead.task_manager.storage_settings, "other-team")
    assert not other["blocks_upgrade"]
    assert other["queued_review_jobs"] == other["claimed_review_jobs"] == []
    with sqlite3.connect(lead.task_manager.storage_settings.sqlite_path) as conn:
        assert "\n".join(conn.iterdump()) == before


@pytest.mark.asyncio
async def test_default_revoked_during_start_cannot_become_ready(lead):
    from custom_agents.agent_instantiation_service import update_runtime_launch
    from custom_messages.contracts.schedule_worker_v1 import AgentLaunchState
    from storage.agent_launch_repository import LaunchConflict

    ship(lead)
    lead._schedule_store.set_enabled("job", True)
    install_schedule_default(lead, lead._schedule_store.get_scheduled_message("job"))
    await ScheduleWorkerController(lead).reconcile()
    launch = lead.starts[0]
    worker = SimpleNamespace(
        **{**vars(lead), "id": launch.agent_id, "_worker_launch": launch}
    )
    get_config().get("agent.schedule_workers")["default_startup_policy_enabled"] = False
    with pytest.raises(LaunchConflict, match="revoked_before_readiness"):
        update_runtime_launch(worker, state=AgentLaunchState.READY)


def test_system_worker_binding_cannot_bypass_exact_schedule_permission(lead):
    from custom_agents.schedule_management import schedule_control_state_digest
    from storage.schedule_worker_repository import ScheduleWorkerConflict

    memory_timer(lead)
    apply_local_schedule_upgrade(lead)
    binding = lead._schedule_store.worker_records.get_binding("job", team_id="team")
    schedule = lead._schedule_store.get_scheduled_message("job")
    with pytest.raises(
        (PermissionError, ValueError, ScheduleWorkerConflict),
        match="System-managed schedule",
    ):
        lead._schedule_store.worker_records.configure(
            binding.model_copy(update={"revision": 2}),
            schedule,
            expected_revision=1,
            expected_control_digest=schedule_control_state_digest(schedule),
        )
    assert (
        lead._schedule_store.worker_records.get_binding("job", team_id="team").revision
        == 1
    )


@pytest.mark.parametrize(
    "blocker",
    [None, "claimed_job", "unfinished_task", "unsupported_runtime", "schedule_claim"],
)
def test_upgrade_command_previews_and_applies_existing_timer(
    lead, tmp_path, monkeypatch, caplog, blocker
):
    import json
    import sqlite3
    import stat
    import yaml
    from config_loader import CONFIG_SCHEMA_VERSION
    from scripts.upgrade_local_schedules import main
    from custom_agents.memory_review_store import MemoryReviewStore

    memory_timer(lead)
    review_store = MemoryReviewStore(
        storage_settings=lead.task_manager.storage_settings
    )
    queued_job = review_store.enqueue_review_job(
        owner_team_id="team",
        owner_named_agent_type="MemReviewAgent",
        task_id="old-audit",
        conversation_id="old-audit-thread",
        turn_index=0,
        source_payload={"review_kind": "coremem_audit", "facts": []},
    )
    if blocker == "claimed_job":
        queued_job = review_store.claim_review_job(
            queued_job.review_job_id,
            owner_team_id="team",
            owner_named_agent_type="MemReviewAgent",
            claimer_agent_id="old-worker",
        )
    elif blocker in {"unfinished_task", "unsupported_runtime"}:
        from storage.runtime_state_repository import create_runtime_state_repository
        from tests.runtime_snapshot_fixtures import snapshot_json

        create_runtime_state_repository(
            lead.task_manager.storage_settings
        ).save_task_runtime(
            expect_existing=False,
            owner_agent_id="old-worker",
            owner_agent_name="Old memory worker",
            team_id="team",
            owner_named_agent_type="MemReviewAgent",
            runtime_state_json=snapshot_json("old-worker-task"),
        )
        if blocker == "unsupported_runtime":
            # The old writer saved the whole task document in the owner row.
            # No current task rows must not be mistaken for a drained worker.
            with sqlite3.connect(
                lead.task_manager.storage_settings.sqlite_path
            ) as conn:
                conn.execute(
                    "DELETE FROM active_task_runtime_records WHERE owner_agent_id=?",
                    ("old-worker",),
                )
                conn.execute(
                    "UPDATE active_task_runtime SET runtime_state_json=? WHERE owner_agent_id=?",
                    (snapshot_json("PRIVATE_OLD_TASK"), "old-worker"),
                )
    elif blocker == "schedule_claim":
        assert lead._schedule_store.claim_next_due(
            claimer_agent_id="old-manager",
            claimer_named_agent_type="TeamArchitect",
            claimer_team_id="team",
        )
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
    caplog.set_level("INFO", logger="upgrade_local_schedules")
    with sqlite3.connect(lead.task_manager.storage_settings.sqlite_path) as conn:
        before = "\n".join(conn.iterdump())
    monkeypatch.setattr("sys.argv", base)
    assert main() == (1 if blocker else 0)
    with sqlite3.connect(lead.task_manager.storage_settings.sqlite_path) as conn:
        assert "\n".join(conn.iterdump()) == before
    assert queued_job.review_job_id in caplog.text
    assert "move_memory_timer" in caplog.text
    assert (
        lead._schedule_store.get_scheduled_message("job").claim_named_agent_type
        == "TeamArchitect"
    )
    backup = tmp_path / "before.json"
    monkeypatch.setattr(
        "sys.argv", base + ["--apply", "--runtime-stopped", "--backup", str(backup)]
    )
    assert main() == (1 if blocker else 0)
    assert review_store.get_review_job(queued_job.review_job_id) == queued_job
    if blocker:
        assert "Upgrade blocked; no changes were made" in caplog.text
        if blocker == "unfinished_task":
            assert "old-worker-task" in caplog.text
            assert "old-worker" in caplog.text
        if blocker == "unsupported_runtime":
            assert '"owner_agent_id": "old-worker"' in caplog.text
            assert "PRIVATE_OLD_TASK" not in caplog.text
            assert "snapshot upgrade procedure" in caplog.text
        assert not backup.exists()
        assert (
            lead._schedule_store.get_scheduled_message("job").claim_named_agent_type
            == "TeamArchitect"
        )
        with sqlite3.connect(lead.task_manager.storage_settings.sqlite_path) as conn:
            assert "\n".join(conn.iterdump()) == before
        return
    assert (
        json.loads(backup.read_text())["schedules"][0]["claim_named_agent_type"]
        == "TeamArchitect"
    )
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert (
        lead._schedule_store.get_scheduled_message("job").claim_named_agent_type
        == "MemReviewAgent"
    )
    assert review_store.get_review_job(queued_job.review_job_id) == queued_job

    # The real local preparation path picks up the original job instead of
    # generating a replacement, even for a legacy team-owned audit without a
    # canonical_user_id. No broker dispatch is involved.
    from custom_agents.scheduled_runtime import prepare_memory_review_schedule
    from storage.pinned_memory_repository import SqlitePinnedMemoryRepository

    pinned = SqlitePinnedMemoryRepository(
        lead.task_manager.storage_settings.sqlite_path
    )
    pinned.create_coremem(team_id="team", fact_text="Test saved memory.", capacity=10)
    worker = SimpleNamespace(
        **{
            **vars(lead),
            "named_agent_type": "MemReviewAgent",
            "_memory_review_store": review_store,
            "_pinned_memory_repository": pinned,
            "memory_config": {"review": {"enabled": True}},
        }
    )
    prepared = prepare_memory_review_schedule(
        worker, "job", lead._schedule_store.get_scheduled_message("job")
    )
    assert queued_job.review_job_id in prepared.payload.problem_statement
    assert review_store.list_review_jobs() == [queued_job]
    assert "get_memory_review_job" in prepared.payload.problem_statement
    assert "source_payload" not in prepared.payload.problem_statement
    assert "Saved review jobs:" not in prepared.payload.problem_statement
