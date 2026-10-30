"""The ``schedules`` transfer section: schedules, their sessions and targets, the heartbeat list and worker setup.

One real old instance with SQLite stores is exported with the real sections
and imported into a fresh root; everything is read back through the new
instance's stores.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from access_control.identity import ISSUER_GATEWAY, ContextSigner, generate_signing_key, public_entry, verify_keys_value
from custom_agents.heartbeat_store import HeartbeatStore
from custom_agents.schedule_execution_identity import execution_digest
from custom_agents.schedule_management import (
    ScheduleMutationAuthorityMode,
    build_schedule_mutation_permit,
    schedule_control_state_digest,
)
from custom_agents.schedule_store import ScheduleStore
from custom_messages.contracts.routing_v1 import GatewayContext, MessageMeta
from custom_messages.contracts.schedule_v1 import (
    ScheduledMessage,
    ScheduleMemoryLedger,
    ScheduleMemoryPolicy,
    ScheduleTaskMemory,
)
from custom_messages.contracts.schedule_worker_v1 import ScheduleWorkerBindingV1, ScheduleWorkerPolicySpecV1
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ScheduleManagementClass, SystemScheduleKind
from internal_credentials import parse_env_file
from runtime_hosting.execution_contracts import (
    AutomationJobV2,
    AutomationScheduleKind,
    AutomationScheduleV1,
    OwnerScopeKind,
    OwnerScopeV1,
)
from storage.execution_control_repository import create_execution_control_repository
from storage.gateway_state_repository import create_gateway_state_repository
from storage.worker_pool_repository import WorkerPoolRepository
from team_transfer.container import TransferReader
from team_transfer.crypto import new_key_derivation
from team_transfer.people import PeopleSection
from team_transfer.schedules import SchedulesSection
from team_transfer.sections import RecordOutcome
from team_transfer.service import export_source, export_team, import_team
from team_transfer.settings import SettingsSection
from tests.pinned_memory_support import new_instance

PASSPHRASE = "correct horse battery staple"
FAST_KEYS = {"opslimit": 1, "memlimit": 8192}
SECTIONS = (SettingsSection(), PeopleSection(), SchedulesSection())
TEAM = "team_a"
LEAD = "TeamArchitect"
WORKER = "WebResearchAgent"
FAR_FUTURE = "2099-01-01T00:00:00+00:00"
LONG_AGO = "2020-01-01T00:00:00+00:00"
TELEGRAM = {"channel_name": "telegram", "user_id": "tg-42", "chat_id": "42"}


@pytest.fixture(autouse=True)
def _fresh_verifier():
    from custom_agents import task_context_runtime

    task_context_runtime._verifier.cache_clear()
    yield
    task_context_runtime._verifier.cache_clear()


def _schedule(schedule_id: str, owner: str | None, *, signer: ContextSigner | None = None, session: str = "sess_ana", **fields: Any) -> ScheduledMessage:
    context = GatewayContext(
        gateway_instance_name="alpha",
        canonical_user_id=owner,
        session_key=session,
        delivery_mode="proactive",
        **TELEGRAM,
    )
    if signer is not None and owner:
        context.task_context = signer.issue(team_id=TEAM, person_id=owner, person_roles=("owner",), schedule_id=schedule_id)
    values: dict[str, Any] = {
        "schedule_id": schedule_id,
        "schedule_name": schedule_id,
        "management_class": ScheduleManagementClass.USER,
        "payload": Task(task_type=MessageMetaType.REQUEST, problem_statement=f"Run {schedule_id}"),
        "payload_meta": MessageMeta(gateway_context=context),
        "claim_team_id": TEAM,
        "claim_named_agent_type": LEAD,
        "interval_ms": 3_600_000,
        "repeat_count": None,
        "next_fire_at": LONG_AGO,
    }
    values.update(fields)
    return ScheduledMessage(**values)


def _system(schedule_id: str, kind: SystemScheduleKind, owner: str = "usr_ana", **fields: Any) -> ScheduledMessage:
    return _schedule(schedule_id, owner, management_class=ScheduleManagementClass.SYSTEM, system_schedule_kind=kind, **fields)


def _people(config: Any) -> Any:
    repository = create_gateway_state_repository(config.state_storage_settings)
    for person, role, status in (("usr_ana", "owner", "active"), ("usr_ben", "member", "active"), ("usr_gone", "member", "removed")):
        repository.create_gateway_user(canonical_user_id=person, display_name=person, timezone="UTC")
        repository.create_team_membership(canonical_user_id=person, team_id=TEAM, role=role, status=status)
    return repository


def _sessions(repository: Any) -> None:
    for key, owner, alias in (("sess_ana", "usr_ana", "garden"), ("sess_ana_other", "usr_ana", None), ("sess_gone", "usr_gone", None)):
        repository.upsert_session(
            session_key=key, session_alias=alias, conversation_id=f"conv_{key}", last_task_id=f"task_{key}",
            manager_agent_id="mgr_old", status="active", owner_canonical_user_id=owner, **TELEGRAM,
        )
    repository.set_active_session_for_channel(session_key="sess_ana", **TELEGRAM)
    repository.upsert_scheduled_target(
        scheduled_job_id="sched_daily", session_key="sess_ana", conversation_id="conv_sched", owner_canonical_user_id="usr_ana"
    )


def _schedules(store: ScheduleStore, signer: ContextSigner, heartbeat_source: str) -> None:
    daily = _schedule(
        "sched_daily", "usr_ana", signer=signer,
        schedule_memory=ScheduleTaskMemory(policy=ScheduleMemoryPolicy(notify="always"), ledger=ScheduleMemoryLedger(total_runs=5)),
    )
    launcher = _system("heartbeat_launcher", SystemScheduleKind.HEARTBEAT_LAUNCHER, schedule_name="heartbeat:alpha")
    launcher.payload_meta.gateway_context.heartbeat_source_id = heartbeat_source
    for schedule in (
        daily,
        _schedule("sched_paused", "usr_ana", signer=signer, interval_ms=None, cron_expression="0 9 * * *",
                  cron_timezone="Europe/Lisbon", paused_until=FAR_FUTURE, next_fire_at=None),
        _schedule("sched_once_past", "usr_ana", signer=signer, interval_ms=None, repeat_count=1),
        _schedule("sched_once_future", "usr_ana", signer=signer, interval_ms=None, repeat_count=1, next_fire_at=FAR_FUTURE),
        _schedule("sched_gone", "usr_gone", signer=signer, session="sess_gone"),
        _schedule("sched_unvouched", "usr_ana"),
        _schedule("sched_worker", "usr_ana", signer=signer, claim_named_agent_type=WORKER),
        launcher,
        _system("sched_aar", SystemScheduleKind.AFTER_ACTION_REPORT_REVIEW, interval_ms=None, repeat_count=1, next_fire_at=FAR_FUTURE),
        _system("mem_rev_core", SystemScheduleKind.MEMORY_REVIEW_COREMEM, enabled=False),
        _system("sched_sys_worker", SystemScheduleKind.MEMORY_REVIEW_USERMEM, claim_named_agent_type=WORKER),
    ):
        store.add(schedule)


def _binding(store: ScheduleStore, schedule_id: str, **fields: Any) -> None:
    stored = store.get_scheduled_message(schedule_id)
    binding = ScheduleWorkerBindingV1(
        schedule_id=schedule_id, team_id=TEAM, owner_canonical_user_id="usr_ana", target_agent_type=WORKER,
        definition_digest="d" * 64, execution_digest=execution_digest(stored, "d" * 64), revision=1,
        policy=ScheduleWorkerPolicySpecV1(), actor_principal_id="lead", authority_mode="agreement", **fields,
    )
    permit = build_schedule_mutation_permit(
        schedule=stored, operation="update", authority_mode=ScheduleMutationAuthorityMode.TRUSTED_RUNTIME,
        actor_principal_id="runtime", owner_canonical_user_id="usr_ana", team_id=TEAM, candidate=stored,
    )
    store.worker_records.configure(
        binding, stored, expected_revision=0, expected_control_digest=schedule_control_state_digest(stored),
        system_mutation_permit=permit,
    )


def _workers_and_pools(config: Any, store: ScheduleStore) -> None:
    _binding(store, "sched_worker", blocked_reason="agent_definition_changed")
    _binding(store, "sched_sys_worker")
    pools = WorkerPoolRepository(config.manager_storage_settings)
    later = datetime.now(timezone.utc) + timedelta(days=2)
    for scope, size, until in (
        ("*", 4, None),
        (WORKER, 2, None),
        (WORKER, 5, later),
        ("GhostAgent", 1, None),
        ("PersonalAdminAgent", 3, datetime.now(timezone.utc) - timedelta(days=1)),
    ):
        pools.set_limit(team_id=TEAM, scope=scope, max_workers=size, until_utc=until, set_by="TeamArchitect", reason="load")


def _heartbeat_and_execution(config: Any) -> None:
    heartbeat = HeartbeatStore(storage_settings=config.manager_storage_settings)
    source = config.heartbeat.source_id
    heartbeat.upsert_source(source_id=source, title="Heartbeat", enabled=True)
    heartbeat.add_item(source_id=source, title="Water", instruction="Water the plants.", conversation_id="conv_hb",
                       heartbeat_item_id="hb_water", source_key="key_water")
    heartbeat.add_item(source_id=source, title="Goal", instruction="Advance the goal.", conversation_id="conv_hb2",
                       heartbeat_item_id="hb_goal", objective_id="obj_missing")
    owner = OwnerScopeV1(scope_kind=OwnerScopeKind.TEAM, owner_id=TEAM, team_id=TEAM)
    create_execution_control_repository(config.manager_storage_settings).create_job(
        AutomationJobV2(
            job_id="exec_job_1", name="Nightly export", owner=owner, revision_id="exec_rev_1", revision_digest="e" * 64,
            schedule=AutomationScheduleV1(schedule_kind=AutomationScheduleKind.INTERVAL, interval_seconds=3600),
        )
    )


def _old_instance(tmp_path: Path) -> tuple[Path, ScheduleStore]:
    path, config = new_instance(tmp_path, "alpha", TEAM)
    old_key, _entry = generate_signing_key(ISSUER_GATEWAY)
    config.layout.team_env_path.parent.mkdir(parents=True, exist_ok=True)
    config.layout.team_env_path.write_text(f"ACCESS_GATEWAY_SIGNING_KEY={old_key}\n", encoding="utf-8")
    _sessions(_people(config))
    store = ScheduleStore(storage_settings=config.manager_storage_settings, default_cron_timezone="UTC")
    _schedules(store, ContextSigner.from_value(ISSUER_GATEWAY, old_key), config.heartbeat.source_id)
    _workers_and_pools(config, store)
    _heartbeat_and_execution(config)
    return path, store


def _transfer(tmp_path: Path, config_path: Path) -> tuple[Any, Any]:
    from assistant_gateway.config import GatewayInstanceConfig

    out = tmp_path / "team.uatransfer"
    export_team(
        export_source(config_path), out, PASSPHRASE, owner_person_id="usr_ana", sections=SECTIONS,
        key_derivation=new_key_derivation(**FAST_KEYS),
    )
    with TransferReader.open(out, PASSPHRASE) as reader:
        plan = import_team(reader, tmp_path / "new", dry_run=False, sections=SECTIONS)
    return plan, GatewayInstanceConfig.load(tmp_path / "new" / "instance_config.yaml")


@pytest.fixture
def moved(tmp_path: Path) -> tuple[dict[str, Any], Any, ScheduleStore, ScheduleStore]:
    config_path, old_store = _old_instance(tmp_path)
    plan, config = _transfer(tmp_path, config_path)
    outcomes = {r.key: r for r in plan.records if r.section == "schedules"}
    new_store = ScheduleStore(storage_settings=config.manager_storage_settings, default_cron_timezone="UTC")
    return outcomes, config, old_store, new_store


def _outcome(outcomes: dict[str, Any], key: str) -> RecordOutcome:
    return outcomes[key].outcome


def test_transfer_keeps_zero_remaining_runs_so_an_exhausted_job_cannot_replay(tmp_path: Path) -> None:
    config_path, source = _old_instance(tmp_path)
    schedule = source.get_scheduled_message("sched_once_future")
    source.add(schedule, remaining_count_override=0)
    _plan, config = _transfer(tmp_path, config_path)
    destination = ScheduleStore(storage_settings=config.manager_storage_settings)
    row = destination.get_row("sched_once_future")
    assert row is None or row["remaining_count"] == 0
    destination.prune_expired()
    assert destination.get_scheduled_message("sched_once_future") is None


def test_a_persons_recurring_schedule_arrives_signed_by_the_new_gateway(moved: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from custom_agents.task_context_runtime import gateway_vouches

    outcomes, config, old_store, new_store = moved
    assert _outcome(outcomes, "schedule/sched_daily") is RecordOutcome.IMPORT
    schedule = new_store.get_scheduled_message("sched_daily")
    assert schedule.enabled
    next_run = datetime.fromisoformat(schedule.next_fire_at)
    assert datetime.now(timezone.utc) < next_run <= datetime.now(timezone.utc) + timedelta(hours=1), "computed from now"
    assert schedule.schedule_memory.policy.notify == "always" and schedule.schedule_memory.ledger.total_runs == 0

    # Derived from the new signing key, as the launcher does (docker/launch_headless.with_verify_keys).
    new_key = parse_env_file(config.layout.team_env_path)["ACCESS_GATEWAY_SIGNING_KEY"]
    monkeypatch.setenv("ACCESS_CONTEXT_VERIFY_KEYS", verify_keys_value([public_entry(ISSUER_GATEWAY, new_key)]))
    new_context = schedule.payload_meta.gateway_context
    old_context = old_store.get_scheduled_message("sched_daily").payload_meta.gateway_context
    assert gateway_vouches(new_context, "usr_ana")
    assert not gateway_vouches(old_context, "usr_ana"), "the old signature does not verify under the new key"
    assert new_context.task_context.context.person_roles == ("admin", "owner")
    assert new_context.task_context.context.schedule_id == "sched_daily"


def test_pause_one_time_runs_and_unvouched_schedules(moved: Any) -> None:
    outcomes, _config, _old, new_store = moved
    paused = new_store.get_scheduled_message("sched_paused")
    assert (paused.paused_until, paused.cron_timezone, paused.enabled) == (FAR_FUTURE, "Europe/Lisbon", True)

    assert _outcome(outcomes, "schedule/sched_once_past") is RecordOutcome.ATTENTION
    assert "its one run was due during the move" in outcomes["schedule/sched_once_past"].reason
    assert new_store.get_scheduled_message("sched_once_past").enabled is False
    future = new_store.get_scheduled_message("sched_once_future")
    assert (future.enabled, future.next_fire_at) == (True, FAR_FUTURE)

    assert _outcome(outcomes, "schedule/sched_unvouched") is RecordOutcome.ATTENTION
    assert "nobody vouched for its owner" in outcomes["schedule/sched_unvouched"].reason
    assert new_store.get_scheduled_message("sched_unvouched").payload_meta.gateway_context.task_context is None


def test_schedules_that_do_not_travel_are_listed(moved: Any) -> None:
    outcomes, _config, _old, new_store = moved
    assert "usr_gone, who is not imported" in outcomes["schedule/sched_gone"].reason
    assert "sets it up again at every start" in outcomes["schedule/heartbeat_launcher"].reason
    assert "one-time follow-up of work that does not move" in outcomes["schedule/sched_aar"].reason
    for schedule_id in ("sched_gone", "heartbeat_launcher", "sched_aar"):
        assert _outcome(outcomes, f"schedule/{schedule_id}") is RecordOutcome.REFUSED
        assert new_store.get_scheduled_message(schedule_id) is None

    assert _outcome(outcomes, "schedule/mem_rev_core") is RecordOutcome.IMPORT
    assert new_store.get_scheduled_message("mem_rev_core").enabled is False, "the operator's off switch is kept"


def test_session_routing_arrives_without_its_conversation(moved: Any) -> None:
    outcomes, config, _old, _new = moved
    repository = create_gateway_state_repository(config.state_storage_settings)
    session = repository.get_session("sess_ana")
    assert (session.session_alias, session.owner_canonical_user_id, session.chat_id) == ("garden", "usr_ana", "42")
    assert (session.conversation_id, session.last_task_id) == (None, None)
    assert repository.get_active_session_for_channel(**TELEGRAM).session_key == "sess_ana"
    assert "session/sess_ana_other" not in outcomes, "only sessions something uses travel"
    assert _outcome(outcomes, "session/sess_gone") is RecordOutcome.REFUSED
    assert repository.get_session("sess_gone") is None
    target = repository.get_scheduled_target("sched_daily")
    assert (target.session_key, target.conversation_id) == ("sess_ana", "conv_sched")


def test_worker_setup_heartbeat_and_pool_sizes(moved: Any) -> None:
    outcomes, config, _old, new_store = moved
    assert _outcome(outcomes, "worker_binding/sched_worker") is RecordOutcome.ATTENTION
    assert "was waiting for review" in outcomes["worker_binding/sched_worker"].reason
    binding = new_store.worker_records.get_binding("sched_worker", team_id=TEAM)
    assert (binding.revision, binding.blocked_reason, binding.target_agent_type) == (1, None, WORKER)
    schedule = new_store.get_scheduled_message("sched_worker")
    assert execution_digest(schedule, binding.definition_digest) == binding.execution_digest
    assert _outcome(outcomes, "worker_binding/sched_sys_worker") is RecordOutcome.REFUSED
    assert "TeamLead sets up its worker again" in outcomes["worker_binding/sched_sys_worker"].reason
    assert new_store.worker_records.get_binding("sched_sys_worker", team_id=TEAM) is None

    heartbeat = HeartbeatStore(storage_settings=config.manager_storage_settings)
    assert heartbeat.get_source(config.heartbeat.source_id).enabled
    items = heartbeat.list_items(config.heartbeat.source_id)
    assert [(item.heartbeat_item_id, item.source_key) for item in items] == [("hb_water", "key_water")]
    assert "objective obj_missing is not imported" in outcomes["heartbeat_item/hb_goal"].reason

    limits = {limit.scope: limit for limit in WorkerPoolRepository(config.manager_storage_settings).list_limits(TEAM)}
    assert set(limits) == {"*", WORKER}
    assert limits["*"].lasting_max_workers == 4
    assert (limits[WORKER].lasting_max_workers, limits[WORKER].temporary_max_workers) == (2, 5)
    assert "GhostAgent does not exist" in outcomes["pool_limit/GhostAgent"].reason
    assert "temporary size, which has ended" in outcomes["pool_limit/PersonalAdminAgent"].reason

    assert _outcome(outcomes, "execution_job/exec_job_1") is RecordOutcome.REFUSED
    assert "not carried by the team transfer yet" in outcomes["execution_job/exec_job_1"].reason
