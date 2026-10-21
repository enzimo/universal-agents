"""Worker policy and controller behavior with real stores and a fake host adapter.

The lead's task carries a gateway-signed context for the schedule's owner, as
every task a person asks for does; schedule tools act only for that person.
"""

import asyncio
import logging
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from config_loader import Config
from custom_agents.agent_instantiation_service import (
    AgentInstantiationService,
    current_worker_limits,
)
from custom_agents.schedule_changes import WorkerConfigurationRequestV1
from custom_agents.schedule_store import ScheduleStore
from custom_agents.schedule_worker_controller import ScheduleWorkerController
from custom_agents.schedule_worker_service import ScheduleWorkerService
from custom_agents.system_schedule_changes import configure_worker
from custom_agents.task_manager import TaskManager
from custom_messages.contracts.routing_v1 import (
    GatewayContext,
    MessageHeader,
    MessageMeta,
)
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.contracts.schedule_worker_v1 import (
    AgentLaunchResultV1,
    ScheduleCapabilityRequirementsV1,
    ScheduleWorkerPolicySpecV1,
    utc_now,
)
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import (
    MessageMetaType,
    ProgressState,
    ScheduleManagementClass,
)
from tests.broker_access_support import context_signer
from tests.change_approval_support import CEDAR_BINARY, needs_cedar, use_signed_contexts


def owner_context(person_id: str = "owner", team_id: str = "team") -> GatewayContext:
    """The gateway context of a task the person asked for, signed by the gateway."""

    return GatewayContext(
        canonical_user_id=person_id,
        task_context=context_signer().issue(team_id=team_id, person_id=person_id, person_roles=("owner",)),
    )


def worker_definition(
    name: str, *, source: str = "team", prompt: str = "Read report", tools=()
) -> dict:
    """A catalog-shaped runtime definition, as load_agent_type_map returns it."""
    root = "/app/ua_definitions/agent_bundles" if source == "release" else "/data/control/agent_bundles"
    return {
        "named_agent_type": name,
        "role": "worker",
        "goal": prompt,
        "tools": list(tools),
        "agent_definition_source": source,
        "agent_definition_source_path": f"{root}/{name}/agent.yaml",
        "declared_definition": {
            "role": "worker",
            "own_prompt": prompt,
            "include_shared_prompt": False,
            "backstory": "hub_and_spoke",
            "default_tools": ["fn:publishing"],
            "skills_enabled": True,
        },
    }


def edit_declared(lead, name: str, **declared) -> None:
    """Simulate someone editing what a team definition file declares."""
    definition = lead.definitions[name]
    changed = {**definition["declared_definition"], **declared}
    lead.definitions[name] = {
        **definition,
        "goal": changed["own_prompt"],
        "declared_definition": changed,
    }


def change_code_expansion(lead, name: str, *, tools=(), shared_prompt: str = "") -> None:
    """Simulate a release that changes a tool set's members or the shared prompt only."""
    definition = lead.definitions[name]
    lead.definitions[name] = {
        **definition,
        "goal": f"{shared_prompt}\n\n{definition['declared_definition']['own_prompt']}",
        "tools": [*definition["tools"], *tools],
    }


@pytest.fixture
def lead(tmp_path, monkeypatch):
    import custom_tools.registry  # noqa: F401

    use_signed_contexts(monkeypatch)

    config = Config(
        {
            "agent": {
                "schedule_workers": {"eligible_agent_types": ["Worker", "Replacement"]}
            }
        }
    )
    monkeypatch.setattr("config_loader._config", config)
    manager = TaskManager(db_path=str(tmp_path / "runtime.db"))
    manager.set_owner_identity(
        agent_id="lead",
        agent_name="lead",
        team_id="team",
        named_agent_type="TeamArchitect",
    )
    manager.add_task(
        Task(
            task_id="request",
            task_type=MessageMetaType.REQUEST,
            problem_statement="Schedule a report",
        ),
        MessageHeader(),
        MessageMeta(gateway_context=owner_context()),
        owner_person_id="owner",
    )
    manager.update_main_task_state(
        "request", ProgressState.IN_PROGRESS, utc_now().isoformat()
    )
    definitions = {
        name: worker_definition(name) for name in ("Worker", "Replacement")
    }
    starts = []

    def spawn(agent_type, team_id, *, launch, **kwargs):
        starts.append(launch)
        return AgentLaunchResultV1(
            status="starting", agent_id=launch.agent_id, launch_id=launch.launch_id
        )

    agent = SimpleNamespace(
        id="lead",
        name="lead",
        named_agent_type="TeamArchitect",
        team_id="team",
        is_team_lead=True,
        workspace_dir=str(tmp_path),
        team_composition_path=None,
        instance_config_path=None,
        mcp_profiles={},
        authorization_config={"enabled": False},
        task_manager=manager,
        _schedule_store=ScheduleStore(storage_settings=manager.storage_settings),
        _logger=logging.getLogger(__name__),
        _scheduler_wake=asyncio.Event(),
        schedule_workers={},
        _probe_live_named_agent_type=AsyncMock(return_value=None),
        _local_team_runtime=SimpleNamespace(
            agent_types=lambda: tuple(definitions),
            local_workers=lambda team_id: (),
            resolve_definition=definitions.get,
            spawn_agent=spawn,
            inspect_launch=lambda record: "starting",
            stop_launch=lambda record: False,
        ),
        starts=starts,
        definitions=definitions,
    )
    schedule = ScheduledMessage(
        schedule_id="job",
        management_class=ScheduleManagementClass.USER,
        payload=Task(
            task_type=MessageMetaType.REQUEST, problem_statement="Read report"
        ),
        payload_meta=MessageMeta(
            gateway_context=GatewayContext(canonical_user_id="owner")
        ),
        claim_team_id="team",
        claim_named_agent_type="Worker",
        interval_ms=60_000,
        enabled=False,
        next_fire_at=(utc_now() - timedelta(minutes=2)).isoformat(),
    )
    agent._schedule_store.add(schedule)
    return agent


def configure(lead, **changes):
    fields = dict(
        target_agent_type="Worker",
        expected_revision=0,
        activate_schedule=True,
        policy=ScheduleWorkerPolicySpecV1(),
        capabilities=ScheduleCapabilityRequirementsV1(),
    )
    return configure_worker(
        lead, WorkerConfigurationRequestV1(schedule_id="job", **{**fields, **changes})
    )


@pytest.mark.asyncio
async def test_changed_definition_tells_the_owner_once_across_worker_and_controller(lead, caplog):
    """The block is recorded and logged once, and its owner is told once, however
    many passes see it and whichever check records it."""
    from custom_agents.schedule_worker_controller import schedule_ready_for_admission
    from custom_agents.stuck_schedules import report_stuck_schedules

    recurring = lead._schedule_store.get_scheduled_message("job")
    recurring.repeat_count = None  # a one-time run this late would be turned off as missed
    lead._schedule_store.add(recurring)
    configure(lead)
    edit_declared(lead, "Worker", own_prompt="A changed worker definition")
    lead.team_comms = SimpleNamespace(send_message=AsyncMock())
    schedule = lead._schedule_store.get_scheduled_message("job")
    later = utc_now() + timedelta(hours=2)
    with caplog.at_level(logging.WARNING):
        for second in range(4):
            await ScheduleWorkerController(lead).reconcile()
            assert not schedule_ready_for_admission(lead, schedule)
            await report_stuck_schedules(lead, now=later + timedelta(seconds=second))
    binding = lead._schedule_store.worker_records.get_binding("job", team_id=lead.team_id)
    assert binding.blocked_reason == "agent_definition_changed_review_required"
    assert caplog.text.count("Scheduled worker needs review.") == 1
    assert caplog.text.count("event=stuck_work_notified") == 1
    assert "schedule_id=job" in caplog.text
    assert "reason=agent_definition_changed_review_required" in caplog.text
    assert lead._schedule_store.get_scheduled_message("job").next_fire_at == schedule.next_fire_at


@pytest.mark.asyncio
async def test_inspection_repairs_custom_eligibility_through_real_settings_and_binding(lead, tmp_path, monkeypatch):
    from assistant_gateway.config import GatewayInstanceConfig
    from custom_agents.runtime_settings_service import RuntimeSettingsService
    from custom_agents.schedule_worker_inspection import inspect_schedule_worker

    config = GatewayInstanceConfig.create(
        instance_name="schedule-test", instance_root=str(tmp_path),
        team_id=lead.team_id, timezone="UTC",
    )
    config.authorization.enabled = False
    lead.instance_config_path = str(config.save(tmp_path / "instance_config.yaml"))
    lead.instance_root = str(config.layout.root)
    lead.workspace_dir = str(config.workspace_path)
    before = inspect_schedule_worker(lead, "job")
    assert before["policy_revision"] == 0
    assert before["eligibility"]["registered"]
    assert not before["eligibility"]["eligible"]
    with pytest.raises(PermissionError, match="agent_type_ineligible"):
        configure(lead)
    change = before["eligibility"]["suggested_settings_change"]
    assert change["value"] == ["WebResearchAgent", "PersonalAdminAgent", "Worker"]
    # The settings plan is bound to the verified person of the running task.
    from datetime import datetime, timezone

    from access_control.changes import ChangeApproval
    from custom_agents import access_runtime
    from custom_messages.contracts.access_v2 import ChangeApprovalMode
    from tests.broker_access_support import context_signer, context_verify_keys

    monkeypatch.setenv("ACCESS_CONTEXT_VERIFY_KEYS", context_verify_keys())
    access_runtime._runtime_access.cache_clear()
    lead.authorization_config = {"enabled": False}
    info = lead.task_manager.get_task_info("request")
    info.meta.gateway_context.task_context = context_signer().issue(
        team_id=lead.team_id, person_id="owner", person_roles=("owner",)
    )
    lead._active_execution_task_id = "request"
    settings = RuntimeSettingsService(lead)
    plan = settings.stage([change])
    agreement = ChangeApproval(ChangeApprovalMode.AGREEMENT, "agreement", datetime.now(timezone.utc))
    assert settings.apply(plan["change_id"], agreement)["status"] == "applied"
    access_runtime._runtime_access.cache_clear()
    after = inspect_schedule_worker(lead, "job")
    assert after["eligibility"]["eligible"]
    assert not after["automation_enabled"]  # Candidate status alone installs no policy.
    assert not lead.starts
    configure(lead)
    await ScheduleWorkerController(lead).reconcile()
    assert len(lead.starts) == 1, inspect_schedule_worker(lead, "job")["block_reason"]
    # An operator denial added later defeats the saved candidate and policy.
    config = GatewayInstanceConfig.load(lead.instance_config_path)
    config.agent.schedule_workers.denied_agent_types = ["Worker"]
    config.save(lead.instance_config_path)
    denied = inspect_schedule_worker(lead, "job")["eligibility"]
    assert denied["explicitly_denied"] and not denied["eligible"]
    assert denied["suggested_settings_change"] is None
    await ScheduleWorkerController(lead).reconcile()
    assert len(lead.starts) == 1
    assert lead._schedule_store.worker_records.get_binding("job", team_id="team").blocked_reason == "agent_type_disallowed"


@pytest.mark.parametrize("worker_config,remove_definition,denied", [
    ({"denied_agent_types": ["Worker"]}, False, True),
    ({"enabled": False}, False, False),
    ({}, True, False),
])
def test_unbound_inspection_identifies_distinct_eligibility_blockers(lead, monkeypatch, worker_config, remove_definition, denied):
    from custom_agents.schedule_worker_inspection import inspect_schedule_worker

    monkeypatch.setattr("config_loader._config", Config({"agent": {"schedule_workers": worker_config}}))
    if remove_definition:
        lead.definitions.pop("Worker")
    result = inspect_schedule_worker(lead, "job")["eligibility"]
    assert not result["eligible"]
    assert result["explicitly_denied"] == denied
    assert result["registered"] != remove_definition
    assert result["automatic_launch_enabled"] == worker_config.get("enabled", True)
    assert result["next_steps"]
    if denied or remove_definition:
        assert result["suggested_settings_change"] is None


def test_unbound_worker_inspection_preserves_owner_isolation(lead):
    from custom_agents.schedule_worker_inspection import inspect_schedule_worker

    schedule = lead._schedule_store.get_scheduled_message("job")
    schedule.payload_meta.gateway_context.canonical_user_id = "different-owner"
    lead._schedule_store.add(schedule)
    with pytest.raises(PermissionError, match="schedule_not_found_in_owner_scope"):
        inspect_schedule_worker(lead, "job")


@pytest.mark.parametrize("schedule_name", [
    "memory_review:coremem:test", "memory_review:usermem:test",
    "memory_review:stale_conversations:test", "heartbeat:test",
])
def test_protected_timer_inspection_does_not_suggest_worker_retarget(lead, schedule_name):
    from custom_agents.schedule_worker_inspection import inspect_schedule_worker

    schedule = lead._schedule_store.get_scheduled_message("job")
    schedule.management_class = ScheduleManagementClass.SYSTEM
    schedule.schedule_name = schedule_name
    schedule.claim_named_agent_type = "TeamArchitect"
    lead._schedule_store.add(schedule)
    result = inspect_schedule_worker(lead, "job")
    assert result["management_class"] == "system"
    assert result["worker_configuration_supported"] is False
    assert result["eligibility"] is None
    assert result["target_agent_type"] == "TeamArchitect"
    if schedule_name.startswith("memory_review:"):
        assert result["execution"]["worker_agent_type"] == "MemReviewAgent"
        assert result["execution"]["worker_start"] == "upgrade_required"
        assert result["execution"]["health_verified"] is False
        assert "upgrade_local_schedules.py" in result["repair_instructions"]
    with pytest.raises(PermissionError, match="local_workers_require_supported_root_schedule"):
        configure(lead)
    assert lead._schedule_store.get_scheduled_message("job").claim_named_agent_type == "TeamArchitect"
    assert not lead.starts


def test_policy_activation_is_bounded_and_retarget_preserves_occurrence(lead):
    first = configure(lead)
    assert first["enabled"] and first["readiness"]["status"] == "provisional"
    due = lead._schedule_store.get_scheduled_message("job").next_fire_at
    configure(lead, target_agent_type="Replacement", expected_revision=1)
    updated = lead._schedule_store.get_scheduled_message("job")
    assert (
        updated.claim_named_agent_type == "Replacement" and updated.next_fire_at == due
    )
    with pytest.raises(ValueError, match="operator_limit"):
        configure(
            lead,
            expected_revision=2,
            policy=ScheduleWorkerPolicySpecV1(
                rules={"max_launch_attempts_per_occurrence": 3}
            ),
        )


def _requirements(tool_id: str, operation: str) -> ScheduleCapabilityRequirementsV1:
    return ScheduleCapabilityRequirementsV1.model_validate(
        {"requirements": [{"capability_id": tool_id, "operation": operation}], "runtime_discovery": False}
    )


@pytest.mark.asyncio
async def test_saved_removed_capability_id_blocks_the_schedule_until_resaved(lead):
    """A schedule saved with a removed tool group id pauses; re-saving with a catalog id repairs it."""

    from custom_agents.schedule_repair_guidance import KNOWN_CAPABILITY_BLOCKER, repair_instructions
    from custom_agents.schedule_worker_inspection import inspect_schedule_worker
    from custom_tools.single_page_site_tools import publish_single_page_site

    lead.definitions["Worker"]["tools"] = [publish_single_page_site]
    configure(lead)
    # What an earlier version saved: the requirement names the removed group.
    old = _requirements("builtin:research_comparison", "publish_single_page_site")
    records = lead._schedule_store.worker_records
    binding = records.get_binding("job", team_id="team")
    with records._connect() as conn:
        conn.execute(
            "UPDATE schedule_worker_bindings SET payload_json=? WHERE schedule_id=?",
            (binding.model_copy(update={"capabilities": old}).model_dump_json(), "job"),
        )
        conn.commit()

    await ScheduleWorkerController(lead).reconcile()

    blocked = records.get_binding("job", team_id="team")
    inspection = inspect_schedule_worker(lead, "job")
    assert blocked.blocked_reason == KNOWN_CAPABILITY_BLOCKER
    assert "requirement_not_a_catalog_tool" in repair_instructions(KNOWN_CAPABILITY_BLOCKER)
    assert "configure_scheduled_worker" in repair_instructions(KNOWN_CAPABILITY_BLOCKER)
    assert inspection["readiness"]["checks"][0]["reason"] == "requirement_not_a_catalog_tool"

    configure(
        lead,
        expected_revision=blocked.revision,
        policy=None,
        capabilities=_requirements("fn:publishing", "publish_single_page_site"),
    )

    assert records.get_binding("job", team_id="team").blocked_reason is None


@pytest.mark.parametrize("change", ["owner", "unsigned", "team", "system", "exact"])
def test_configuration_rejects_wrong_authority_and_unsafe_targets(lead, change):
    # Whether the agent may configure workers at all is decided per call by
    # the access hook (configure_scheduled_worker, fn:schedules full); here
    # only the owner check and the schedule's own limits apply.
    context = lead.task_manager.get_task_info("request").meta.gateway_context
    if change == "owner":
        context.task_context = owner_context("other").task_context
    elif change == "unsigned":
        context.task_context = None
    elif change == "team":
        lead.team_id = "other_team"
    else:
        schedule = lead._schedule_store.get_scheduled_message("job")
        schedule.management_class = (
            ScheduleManagementClass.SYSTEM
            if change == "system"
            else ScheduleManagementClass.USER
        )
        schedule.claim_agent_id = "exact-worker" if change == "exact" else None
        lead._schedule_store.add(schedule)
    with pytest.raises((PermissionError, ValueError)):
        configure(lead)


@pytest.mark.asyncio
async def test_controller_starts_once_and_rechecks_later_revocation(lead, monkeypatch):
    configure(lead)
    controller = ScheduleWorkerController(lead)
    await controller.reconcile()
    await controller.reconcile()
    assert len(lead.starts) == 1
    monkeypatch.setattr(
        "config_loader._config",
        Config(
            {
                "agent": {
                    "schedule_workers": {
                        "eligible_agent_types": ["Worker"],
                        "denied_agent_types": ["Worker"],
                    }
                }
            }
        ),
    )
    await controller.reconcile()
    assert len(lead.starts) == 1
    assert (
        lead._schedule_store.worker_records.get_binding(
            "job", team_id="team"
        ).blocked_reason
        == "agent_type_disallowed"
    )
    manual = AgentInstantiationService(lead).launch(
        agent_type="Worker", owner_id="owner", request_id="manual"
    )
    assert not manual.accepted and manual.reason == "agent_type_disallowed"


def test_lower_operator_limits_are_revalidated(lead, monkeypatch):
    monkeypatch.setattr(
        "config_loader._config",
        Config(
            {
                "agent": {
                    "schedule_workers": {
                        "max_claim_wait_seconds": 10,
                        "defaults": {"claim_wait_seconds": 5},
                        "eligible_agent_types": ["Worker"],
                    }
                }
            }
        ),
    )
    assert current_worker_limits(lead).resolve_rules({}).claim_wait_seconds == 5
    with pytest.raises(ValueError, match="operator_limit"):
        configure(
            lead, policy=ScheduleWorkerPolicySpecV1(rules={"claim_wait_seconds": 20})
        )


def enable_access(lead, *, starter_profile: str | None) -> None:
    """Authorization on, with real Cedar and the access store in the task database."""

    from access_control.entities import PersonPrincipal
    from access_control.starter_profiles import apply_starter_profile
    from custom_agents import access_runtime
    from custom_agents.agent_catalog import load_agent_catalog

    shipped = load_agent_catalog(include_configured=False).agent_types["ResearchComparisonAgent"]
    lead.definitions["Worker"]["tools"] = list(shipped.tools)
    lead.authorization_config = {"enabled": True, "cedar_binary_path": str(CEDAR_BINARY)}
    access_runtime._runtime_access.cache_clear()
    if starter_profile:
        service = access_runtime.runtime_access(lead).service
        apply_starter_profile(
            service,
            PersonPrincipal("owner", "team", ("owner",)),
            starter_profile,
            lead_agent_type="TeamArchitect",
        )


WEB_SEARCH = ScheduleCapabilityRequirementsV1.model_validate(
    {"requirements": [{"capability_id": "fn:web_research", "operation": "exa_search"}]}
)
# Publishing is in the standard tool set: the owner holds it, a worker type
# needs a grant for the owner's work, and the trusted profile covers it.
PUBLISH = ScheduleCapabilityRequirementsV1.model_validate(
    {"requirements": [{"capability_id": "fn:publishing", "operation": "publish_single_page_site"}]}
)


@needs_cedar
def test_authorization_hands_on_what_the_lead_may_give_and_names_the_rest(lead):
    from custom_agents import access_runtime

    enable_access(lead, starter_profile=None)
    configure(lead, capabilities=PUBLISH)

    # The lead holds nothing it may hand on: a person must give the access.
    unassisted = ScheduleWorkerService(lead).authorize_access("job", expected_revision=1)
    assert unassisted["given"] == [] and unassisted["waiting"] == []
    asked = unassisted["ask_a_person"][0]
    assert asked["tool_id"] == "fn:publishing"
    # A level per tool, never single operations (owner's decision 2026-09-30).
    assert "request_access(tool_id='fn:publishing', level='write'" in asked["message"]
    assert asked["operations"] == ["publish_single_page_site"] and asked["level"] == "write"

    # With the trusted starter profile the lead hands it on at once and the
    # owner gets a notice; the worker type then holds access that outlasts the
    # task, for the owner's work only (decision D13).
    enable_access(lead, starter_profile="trusted")
    handed = ScheduleWorkerService(lead).authorize_access("job", expected_revision=1)
    assert handed["ask_a_person"] == []
    assert [item["tool_id"] for item in handed["given"]] == ["fn:publishing"]
    grant_id = handed["given"][0]["grant_id"]
    store = access_runtime.runtime_access(lead).service.store
    grant = store.get(grant_id)
    # The schedule holds it: every agent in its runs uses it, and only there.
    assert grant is not None and grant.subject.kind.value == "schedule" and grant.subject.id == "team/job"
    assert grant.task_id is None and grant.for_person_id == "owner"
    # The whole tool at the level the job needs.
    assert grant.target.kind.value == "tool" and grant.target.id == "fn:publishing" and grant.level.value == "write"
    notices = store.notices_for("team", person_id="owner", roles=("owner",))
    assert any(notice.grant_id == grant_id for notice, _read in notices)

    ready = ScheduleWorkerService(lead).prepare("job")
    assert ready["missing_access"] == []
    activated = configure(lead, expected_revision=1, capabilities=None)
    assert activated["enabled"]
    with pytest.raises(ValueError, match="revision_conflict"):
        ScheduleWorkerService(lead).authorize_access("job", expected_revision=1)


@needs_cedar
def test_each_schedule_gets_its_own_access_that_follows_its_requirements(lead):
    from access_control.entities import AgentPrincipal, PersonPrincipal
    from custom_agents import access_runtime
    from custom_messages.contracts.access_v2 import GrantStatus

    enable_access(lead, starter_profile="trusted")
    configure(lead, capabilities=PUBLISH)
    handed = ScheduleWorkerService(lead).authorize_access("job", expected_revision=1)
    first = handed["given"][0]["grant_id"]
    service = access_runtime.runtime_access(lead).service
    owner = PersonPrincipal("owner", "team", ("owner",))
    publish = service.catalog.resolve_function("publish_single_page_site", {})

    def may_publish(schedule_id):
        worker = AgentPrincipal("worker-1", "Worker", "worker", "team", schedule_id=schedule_id)
        return service.decide(agent=worker, person=owner, resolution=publish, task_id="run-1").allowed

    assert may_publish("job")
    assert not may_publish("other-job"), "another schedule of the same worker type does not share it"
    assert not may_publish(None)

    # Setup again after the requirement is dropped: the schedule's access ends.
    configure(lead, expected_revision=1, capabilities=WEB_SEARCH)
    dropped = ScheduleWorkerService(lead).authorize_access("job", expected_revision=2)
    assert first in dropped["ended"] and not may_publish("job")

    # Deleting the schedule ends what it still had.
    configure(lead, expected_revision=2, capabilities=PUBLISH)
    again = ScheduleWorkerService(lead).authorize_access("job", expected_revision=3)
    second = again["given"][0]["grant_id"]
    assert service.store.get(second).status is GrantStatus.ACTIVE
    assert lead._schedule_store.remove("job")
    assert service.store.get(second).status is GrantStatus.REVOKED
    [ended] = [event for event in service.store.events("team", grant_id=second) if event.kind == "grant_revoked"]
    assert ended.detail == {"ended_with_schedule": "job"}


@needs_cedar
def test_setup_ends_only_its_own_schedules_grants_for_the_current_owner(lead):
    """Requirement cleanup preserves other schedules, earlier owners and grants a person gave."""

    from datetime import datetime, timezone

    from access_control.entities import schedule_subject
    from custom_agents import access_runtime
    from custom_messages.contracts.access_v2 import GrantStatus, GrantV1, SubjectKind, SubjectRefV1, TargetKind, TargetRefV1

    enable_access(lead, starter_profile="trusted")
    configure(lead, capabilities=PUBLISH)
    own_grant = ScheduleWorkerService(lead).authorize_access("job", expected_revision=1)["given"][0]["grant_id"]
    service = access_runtime.runtime_access(lead).service
    grantor = access_runtime.acting_agent(lead).agent.type_subject()
    protected = (
        ("grant-earlier-owner", "job", "earlier-owner", grantor),
        ("grant-other-job", "other-job", "owner", grantor),
        ("grant-from-person", "job", "owner", SubjectRefV1(kind=SubjectKind.USER, id="owner")),
    )
    for grant_id, schedule_id, person_id, given_by in protected:
        service.store.create(
            GrantV1(
                grant_id=grant_id, team_id="team", subject=schedule_subject("team", schedule_id),
                target=TargetRefV1(kind=TargetKind.TOOL, id="fn:publishing"),
                granted_by=given_by, created_at_utc=datetime.now(timezone.utc), for_person_id=person_id,
            )
        )

    # Publishing is no longer needed, but the team lead ends only what it gave for this owner's work.
    configure(lead, expected_revision=1, capabilities=WEB_SEARCH)
    result = ScheduleWorkerService(lead).authorize_access("job", expected_revision=2)

    assert own_grant in result["ended"]
    for grant_id, *_ in protected:
        assert grant_id not in result["ended"]
        assert service.store.get(grant_id).status is GrantStatus.ACTIVE


def test_authorization_off_needs_no_hand_on(lead):
    configure(lead, capabilities=WEB_SEARCH)
    result = ScheduleWorkerService(lead).authorize_access("job", expected_revision=1)
    assert result["status"] == "not_needed"
    binding = lead._schedule_store.worker_records.get_binding("job", team_id="team")
    assert binding.authority_mode == "agreement"


@pytest.mark.asyncio
async def test_enabling_authorization_blocks_stale_agreement_launcher(
    lead, monkeypatch
):
    configure(lead)
    monkeypatch.setattr(
        "config_loader._config",
        Config(
            {
                "agent": {"schedule_workers": {"eligible_agent_types": ["Worker"]}},
                "authorization": {"enabled": True},
            }
        ),
    )
    await ScheduleWorkerController(lead).reconcile()
    assert lead.starts == []
    assert (
        lead._schedule_store.worker_records.get_binding(
            "job", team_id="team"
        ).blocked_reason
        == "launcher_authorization_mode_changed_restart_required"
    )


def test_worker_type_can_be_suspended_after_operator_disallows_it(lead, monkeypatch):
    configure(lead)
    monkeypatch.setattr(
        "config_loader._config",
        Config({"agent": {"schedule_workers": {"denied_agent_types": ["Worker"]}}}),
    )
    result = configure(
        lead, expected_revision=1, policy=ScheduleWorkerPolicySpecV1(enabled=False)
    )
    assert not result["automation_enabled"]


def test_an_mcp_requirement_needs_an_installed_server_that_names_the_worker_type(lead, tmp_path):
    """Authorization off: agents load the installed servers whose record names their type (plan 7d)."""

    from custom_agents import access_runtime
    from custom_messages.contracts.access_v2 import ChangeApprovalMode, ChangeApprovalRecordV1
    from mcp_control.registry import (
        MCPConnectionLifecycle,
        MCPConnectionRecordV1,
        MCPConnectionSource,
        MCPConnectionStore,
        MCPOperationDefinitionV1,
        _new_record,
        descriptor_from_server_config,
        resolve_mcp_connection_paths,
    )

    root = tmp_path / "instance"
    lead.instance_root = str(root)
    access_runtime._runtime_access.cache_clear()
    now = utc_now()
    approval = ChangeApprovalRecordV1(mode=ChangeApprovalMode.AGREEMENT, approved_by="agreement", approved_at_utc=now)
    descriptor = descriptor_from_server_config(
        {"name": "crawler", "transport": "streamable_http", "server_url": "https://crawler.example.test/mcp"},
        source=MCPConnectionSource.AGENT_PROPOSAL,
        source_locator="agent-proposal:lead:1",
        team_id="team",
        owner_canonical_user_id="owner",
    )
    store = MCPConnectionStore(resolve_mcp_connection_paths(root))

    def install(*target_agent_types: str) -> None:
        payload = _new_record(
            descriptor,
            lifecycle=MCPConnectionLifecycle.ACTIVE,
            target_agent_types=target_agent_types,
            approval=approval,
            now=now,
        ).model_dump(mode="python")
        for key in ("record_digest", "operation_catalog_sha256"):
            payload.pop(key, None)
        payload["operations"] = (MCPOperationDefinitionV1.build(operation_name="crawl", input_schema={"type": "object"}),)
        store.save(MCPConnectionRecordV1.build(**payload))

    requirement = _requirements("mcp:crawler", "crawl")
    service = ScheduleWorkerService(lead)

    try:
        install("Other")
        unattached = service.prepare("job", capabilities=requirement)
        install("Worker")
        attached = service.prepare("job", capabilities=requirement)
    finally:
        access_runtime._runtime_access.cache_clear()

    assert unattached["checks"][0]["reason"] == "mcp_server_not_for_worker_type"
    assert attached["checks"][0]["status"] == "ready"
    assert attached["checks"][0]["tool_id"] == "mcp:crawler"
    assert attached["checks"][0]["access_level"] == "full", "an unconfirmed MCP operation needs full"


@pytest.mark.asyncio
async def test_configuring_a_worker_from_a_tool_thread_wakes_the_scheduler_on_its_loop(lead):
    """configure_scheduled_worker is a sync tool: it runs in a worker thread, and
    asyncio.Event is not thread-safe, so the scheduler is woken on the agent loop."""
    import threading

    loop_thread = threading.get_ident()
    set_on: list[int] = []

    class RecordingWake(asyncio.Event):
        def set(self) -> None:
            set_on.append(threading.get_ident())
            super().set()

    lead._runtime_loop = asyncio.get_running_loop()
    lead._scheduler_wake = RecordingWake()
    result = await asyncio.to_thread(configure, lead)  # as Strands runs the sync tool
    await asyncio.sleep(0.05)
    assert "error" not in result
    assert set_on == [loop_thread]
    assert lead._scheduler_wake.is_set()
