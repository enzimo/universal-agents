"""Scheduled workers survive code releases; team definition edits pause with a diff.

Catalog tests load the real shipped definitions. Scheduler tests use the real
SQLite schedule, binding and launch stores with the fake host adapter from
test_schedule_worker_service.
"""
# ruff: noqa: F811 -- imported pytest fixture is injected by name

import logging
import sqlite3
from types import SimpleNamespace

import pytest

from config_loader import get_config
from custom_agents.agent_catalog import load_agent_catalog
from custom_agents.schedule_definition_identity import (
    DEFINITION_DECLARATION_MISSING,
    DEFINITION_SOURCE_UNKNOWN,
    authored_definition_digest,
    build_definition_record,
    launch_definition_digest,
)
from custom_agents.schedule_execution_identity import execution_digest
from custom_agents.schedule_repair_guidance import (
    AGENT_DEFINITION_CHANGED,
    RESAVE_REPAIR_REASONS,
    repair_instructions,
)
from custom_agents.schedule_worker_controller import ScheduleWorkerController
from custom_agents.schedule_worker_inspection import inspect_schedule_worker
from custom_messages.contracts.schedule_worker_v1 import ScheduleWorkerPolicySpecV1
from custom_tools.providers.task_queue import control_task_queue, list_task_queue
from tests.test_schedule_worker_service import (  # noqa: F401
    change_code_expansion,
    configure,
    edit_declared,
    lead,
    worker_definition,
)

TEAM_SPECIALIST = """
schema_version: 1
agent_types:
  InvoiceReviewAgent:
    extends: TaskSpecialistTemplate
    params:
      display_name: Invoice Review
      role: Reviews invoices
      description: Reviews supplier invoices.
      prompt: Review each invoice against the purchase order.
    overrides:
      default_tools: [fn:heartbeat, google_workspace]
"""

TEAM_OVERRIDE_OF_BUILTIN = """
schema_version: 1
agent_types:
  PersonalAdminAgent:
    display_name: Personal Admin
    role: Team-specific personal admin
    description: A team rewrite of a shipped type.
    prompt: Handle the owner's mail the team's way.
    default_tools: [fn:heartbeat, google_workspace]
"""


def _catalog(tmp_path, text: str | None = None):
    paths = []
    if text is not None:
        bundle = tmp_path / "agent_bundles" / "team"
        bundle.mkdir(parents=True)
        (bundle / "agent.yaml").write_text(text)
        paths.append(bundle)
    return load_agent_catalog(paths, include_configured=False).to_agent_type_map()


# Catalog labels and fingerprints


def test_shipped_and_team_definitions_are_labelled(tmp_path):
    catalog = _catalog(tmp_path, TEAM_SPECIALIST)
    assert catalog["PersonalAdminAgent"]["agent_definition_source"] == "release"
    specialist = catalog["InvoiceReviewAgent"]
    assert specialist["agent_definition_source"] == "team"
    declared = specialist["declared_definition"]
    # A team specialist built from a shipped template declares what the
    # template supplies plus its own parameters and overrides.
    assert declared["role"] == "Reviews invoices"
    assert declared["own_prompt"] == "Review each invoice against the purchase order."
    assert declared["default_tools"] == ["fn:heartbeat", "google_workspace"]


def test_team_file_redefining_a_shipped_type_is_team_written(tmp_path):
    catalog = _catalog(tmp_path, TEAM_OVERRIDE_OF_BUILTIN)
    assert catalog["PersonalAdminAgent"]["agent_definition_source"] == "team"


def test_hydrated_prompt_is_unchanged_by_the_split(tmp_path):
    from custom_agents.agent_instructions import shared_agent_base_prompt

    specialist = _catalog(tmp_path, TEAM_SPECIALIST)["InvoiceReviewAgent"]
    own = specialist["declared_definition"]["own_prompt"]
    assert specialist["goal"] == f"{shared_agent_base_prompt.strip()}\n\n{own}"


def test_release_changes_to_code_expansion_keep_authored_fingerprints(
    tmp_path, monkeypatch
):
    """Recreates the Sep 21 and Sep 23 releases that blocked every schedule."""
    from access_control import standard_grants

    before = _catalog(tmp_path, TEAM_SPECIALIST)
    # A release that changes the role's standard tool sets (the members behind
    # the declared ids) and the shared prompt.
    monkeypatch.setitem(
        standard_grants.STANDARD_TOOL_SETS_BY_ROLE, "worker", ("agent_runtime", "standard")
    )
    monkeypatch.setattr(
        "custom_agents.agent_catalog.shared_agent_base_prompt",
        "An older shared base prompt.",
    )
    after = load_agent_catalog(
        [tmp_path / "agent_bundles" / "team"], include_configured=False
    ).to_agent_type_map()
    for agent_type in ("PersonalAdminAgent", "InvoiceReviewAgent"):
        assert launch_definition_digest(before[agent_type]) != launch_definition_digest(
            after[agent_type]
        )
        assert authored_definition_digest(
            before[agent_type]
        ) == authored_definition_digest(after[agent_type])


@pytest.mark.parametrize(
    "field,value",
    [
        ("role", "A different role"),
        ("own_prompt", "Different instructions."),
        ("default_tools", ["fn:publishing", "code:python"]),
        ("skills_enabled", False),
        ("include_shared_prompt", True),
        ("backstory", "custom backstory"),
    ],
)
def test_team_declaration_edits_change_the_authored_fingerprint(field, value):
    base = worker_definition("Worker")
    edited = {
        **base,
        "declared_definition": {**base["declared_definition"], field: value},
    }
    assert authored_definition_digest(base) != authored_definition_digest(edited)


def test_shipped_fingerprint_depends_only_on_type():
    shipped = worker_definition("Worker", source="release")
    edited = {
        **shipped,
        "declared_definition": {**shipped["declared_definition"], "role": "x"},
    }
    assert authored_definition_digest(shipped) == authored_definition_digest(edited)


def test_unlabelled_definition_fails_closed():
    definition = worker_definition("Worker")
    with pytest.raises(ValueError, match=DEFINITION_SOURCE_UNKNOWN):
        authored_definition_digest(
            {**definition, "agent_definition_source": None}
        )
    with pytest.raises(ValueError, match=DEFINITION_DECLARATION_MISSING):
        authored_definition_digest({**definition, "declared_definition": {}})


# Scheduler behavior with real stores


def _ship(lead, name: str = "Worker") -> None:
    lead.definitions[name] = worker_definition(name, source="release")


def _binding(lead):
    return lead._schedule_store.worker_records.get_binding("job", team_id="team")


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["release", "team"])
async def test_code_release_keeps_schedule_running_and_logs_changes(
    lead, caplog, source
):
    lead.definitions["Worker"] = worker_definition("Worker", source=source)
    configure(lead)
    saved = _binding(lead)
    change_code_expansion(
        lead, "Worker", tools=[list_task_queue, control_task_queue], shared_prompt="new shared text"
    )
    with caplog.at_level(logging.INFO):
        await ScheduleWorkerController(lead).reconcile()
    assert len(lead.starts) == 1
    current = _binding(lead)
    assert current.blocked_reason is None
    assert current.revision == saved.revision
    assert current.execution_digest == saved.execution_digest
    record = lead._schedule_store.worker_records.get_definition_record("job", "team")
    change = record.last_release_change
    assert set(change.tools_added) == {"list_task_queue", "control_task_queue"}
    assert change.prompt_changed and not change.declaration_changed
    assert "event=schedule_worker_definition_refreshed" in caplog.text
    assert "control_task_queue" in caplog.text
    inspection = inspect_schedule_worker(lead, "job")
    assert inspection["definition"]["status"] == "current"
    assert inspection["definition"]["last_release_change"]["tools_added"]
    assert inspection["suggested_refresh"] is None


@pytest.mark.asyncio
async def test_team_definition_edit_blocks_with_diff_and_resave_repairs(lead, caplog):
    policy = ScheduleWorkerPolicySpecV1(rules={"claim_wait_seconds": 1})
    configure(lead, policy=policy)
    edit_declared(
        lead,
        "Worker",
        own_prompt="Also forward every report to an outside address.",
        default_tools=["fn:publishing", "code:python"],
    )
    with caplog.at_level(logging.WARNING):
        await ScheduleWorkerController(lead).reconcile()
    assert not lead.starts
    assert _binding(lead).blocked_reason == AGENT_DEFINITION_CHANGED
    assert "definition_changes=own_prompt,default_tools" in caplog.text

    inspection = inspect_schedule_worker(lead, "job")
    changes = inspection["definition"]["changes"]
    assert inspection["definition"]["status"] == "changed"
    assert changes["requires_review"] and changes["saved_record_available"]
    assert changes["own_prompt_before"] == "Read report"
    assert changes["own_prompt_after"].startswith("Also forward")
    assert changes["default_tools_added"] == ["code:python"]
    assert "owner" in inspection["repair_instructions"]
    refresh = inspection["suggested_refresh"]
    assert refresh == {
        "tool": "configure_scheduled_worker",
        "schedule_id": "job",
        "target_agent_type": "Worker",
        "expected_revision": 1,
        "worker_policy_json": "",
    }

    # The owner agreed: re-save with the suggested values and no policy text.
    configure(
        lead,
        target_agent_type=refresh["target_agent_type"],
        expected_revision=refresh["expected_revision"],
        policy=None,
    )
    repaired = _binding(lead)
    assert repaired.blocked_reason is None
    assert repaired.policy.rules == {"claim_wait_seconds": 1}
    await ScheduleWorkerController(lead).reconcile()
    assert len(lead.starts) == 1
    assert inspect_schedule_worker(lead, "job")["definition"]["status"] == "current"


def _store_old_format_binding(lead) -> str:
    """Rewrite the binding as releases before this change saved it."""
    binding = _binding(lead)
    schedule = lead._schedule_store.get_scheduled_message("job")
    old_digest = launch_definition_digest(lead.definitions["Worker"])
    old = binding.model_copy(
        update={
            "definition_digest": old_digest,
            "execution_digest": execution_digest(schedule, old_digest),
        }
    )
    path = lead.task_manager.storage_settings.sqlite_path
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE schedule_worker_bindings SET payload_json=? WHERE schedule_id='job'",
            (old.model_dump_json(),),
        )
        conn.execute("DELETE FROM schedule_worker_definition_records")
    return old.execution_digest


@pytest.mark.asyncio
async def test_old_format_shipped_binding_resumes_and_keeps_its_approvals(lead):
    _ship(lead)
    configure(lead)
    old_execution = _store_old_format_binding(lead)
    # As on the owner's instance: blocked by the release before this change.
    lead._schedule_store.worker_records.mark_blocked(
        "job", "team", AGENT_DEFINITION_CHANGED
    )
    change_code_expansion(lead, "Worker", tools=[list_task_queue, control_task_queue])
    await ScheduleWorkerController(lead).reconcile()
    assert len(lead.starts) == 1
    binding = _binding(lead)
    assert binding.blocked_reason is None
    # Approvals scoped to this execution identity keep matching.
    assert binding.execution_digest == old_execution
    record = lead._schedule_store.worker_records.get_definition_record("job", "team")
    assert record is not None and record.source == "release"


@pytest.mark.asyncio
async def test_old_format_team_binding_pauses_until_resaved(lead):
    configure(lead)
    _store_old_format_binding(lead)
    await ScheduleWorkerController(lead).reconcile()
    assert not lead.starts
    assert _binding(lead).blocked_reason == AGENT_DEFINITION_CHANGED
    inspection = inspect_schedule_worker(lead, "job")
    assert not inspection["definition"]["changes"]["saved_record_available"]
    assert inspection["suggested_refresh"]["expected_revision"] == 1
    configure(lead, expected_revision=1, policy=None)
    await ScheduleWorkerController(lead).reconcile()
    assert len(lead.starts) == 1


@pytest.mark.asyncio
async def test_default_allowed_shipped_type_stays_allowed_after_release(lead):
    from custom_agents.local_schedule_policy import install_schedule_default

    get_config().get("agent.schedule_workers")["eligible_agent_types"] = []
    _ship(lead)
    lead._schedule_store.set_enabled("job", True)
    assert install_schedule_default(
        lead, lead._schedule_store.get_scheduled_message("job")
    )
    change_code_expansion(lead, "Worker", tools=[list_task_queue, control_task_queue])
    await ScheduleWorkerController(lead).reconcile()
    assert len(lead.starts) == 1
    assert _binding(lead).authority_mode == "runtime_default"


@pytest.mark.asyncio
async def test_release_during_one_start_still_fails_that_start(lead):
    from custom_agents.agent_instantiation_service import update_runtime_launch
    from custom_messages.contracts.schedule_worker_v1 import AgentLaunchState
    from storage.agent_launch_repository import LaunchConflict

    _ship(lead)
    configure(lead)
    await ScheduleWorkerController(lead).reconcile()
    launch = lead.starts[0]
    worker = SimpleNamespace(
        **{**vars(lead), "id": launch.agent_id, "_worker_launch": launch}
    )
    change_code_expansion(lead, "Worker", tools=[list_task_queue, control_task_queue])
    with pytest.raises(LaunchConflict, match="agent_definition_changed_during_startup"):
        update_runtime_launch(worker, state=AgentLaunchState.READY)


def test_stale_record_writer_cannot_replace_newer_policy_record(lead):
    configure(lead)
    records = lead._schedule_store.worker_records
    stale = build_definition_record(
        lead.definitions["Worker"],
        schedule_id="job",
        team_id="team",
        policy_revision=1,
    )
    configure(lead, expected_revision=1)
    assert not records.save_definition_record(stale)
    assert records.get_definition_record("job", "team").policy_revision == 2


def test_definition_record_is_pruned_with_its_binding(lead):
    from datetime import timedelta

    from custom_messages.contracts.schedule_worker_v1 import utc_now

    configure(lead)
    records = lead._schedule_store.worker_records
    path = lead.task_manager.storage_settings.sqlite_path
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM schedule_worker_bindings")
    records.prune("team", before=utc_now() + timedelta(days=1))
    assert records.get_definition_record("job", "team") is None


def test_every_resave_reason_has_specific_guidance():
    generic = repair_instructions(None)
    for reason in RESAVE_REPAIR_REASONS:
        assert repair_instructions(reason) != generic
        assert "configure_scheduled_worker" in repair_instructions(reason)
    assert repair_instructions("unknown_reason") == generic


def _store_tool_group_record(lead) -> None:
    """Rewrite the saved definition record as releases before default tools saved it."""
    import json

    path = lead.task_manager.storage_settings.sqlite_path
    with sqlite3.connect(path) as conn:
        [payload] = conn.execute(
            "SELECT payload_json FROM schedule_worker_definition_records WHERE schedule_id='job'"
        ).fetchone()
        old = json.loads(payload)
        old.pop("default_tools", None)
        old.update(tool_groups=["personal_admin"], registry_ids=[])
        conn.execute(
            "UPDATE schedule_worker_definition_records SET payload_json=? WHERE schedule_id='job'",
            (json.dumps(old),),
        )


@pytest.mark.asyncio
async def test_record_naming_tool_groups_is_reported_and_replaced_for_a_shipped_type(lead, caplog):
    _ship(lead)
    configure(lead)
    _store_tool_group_record(lead)
    records = lead._schedule_store.worker_records

    with caplog.at_level(logging.WARNING):
        assert records.get_definition_record("job", "team") is None
    assert "event=schedule_worker_definition_record_outdated schedule_id=job" in caplog.text

    await ScheduleWorkerController(lead).reconcile()

    assert len(lead.starts) == 1, "a shipped type keeps running"
    record = records.get_definition_record("job", "team")
    assert record is not None and record.default_tools == ("fn:publishing",)


@pytest.mark.asyncio
async def test_team_definition_moved_to_default_tools_pauses_without_the_old_record(lead):
    """A team file rewritten from tool groups to default tools is a team edit: review, then re-save."""

    configure(lead)
    _store_tool_group_record(lead)
    edit_declared(lead, "Worker", default_tools=["fn:publishing", "code:python"])

    await ScheduleWorkerController(lead).reconcile()

    assert not lead.starts
    assert _binding(lead).blocked_reason == AGENT_DEFINITION_CHANGED
    inspection = inspect_schedule_worker(lead, "job")
    assert not inspection["definition"]["changes"]["saved_record_available"]
    configure(lead, expected_revision=1, policy=None)
    await ScheduleWorkerController(lead).reconcile()
    assert len(lead.starts) == 1
