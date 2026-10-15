"""The ``objectives`` transfer section carries open objectives, installed work profiles and work policies."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from custom_messages.enums import ObjectiveHorizon, ObjectiveStatus, ObjectiveWorkPolicyStatus
from custom_messages.records.objectives import (
    ObjectiveRecord,
    ObjectiveWorkBudget,
    ObjectiveWorkInstalledProfile,
    ObjectiveWorkPolicy,
    ObjectiveWorkProfile,
)
from storage.objective_repository import create_objective_repository
from storage.objective_work_repository import create_objective_work_repository
from team_transfer.container import TransferReader
from team_transfer.crypto import new_key_derivation
from team_transfer.objectives import ObjectivesSection, owner_scope
from team_transfer.people import PeopleSection
from team_transfer.sections import RecordOutcome
from team_transfer.service import export_source, export_team, import_team
from team_transfer.settings import SettingsSection
from tests.pinned_memory_support import new_instance

PASSPHRASE = "correct horse battery staple"
FAST_KEYS = {"opslimit": 1, "memlimit": 8192}
SECTIONS = (SettingsSection(), PeopleSection(), ObjectivesSection())
TEAM = "team_a"
BUDGET = ObjectiveWorkBudget(active_seconds=600, checkpoint_reserve_seconds=60, turns=10, total_tokens=50_000, tool_calls=20)


def _objective(objective_id: str, scope: str, owner: str | None, status: ObjectiveStatus = ObjectiveStatus.ACTIVE) -> ObjectiveRecord:
    return ObjectiveRecord(
        objective_id=objective_id,
        title=objective_id,
        desired_outcome="Keep the garden watered.",
        why="Plants need water.",
        status=status,
        horizon=ObjectiveHorizon.LONG_HORIZON,
        owner_agent_id=scope,
        owner_canonical_user_id=owner,
        team_id=TEAM,
    )


def _profile(profile_id: str, scope: str, owner: str) -> ObjectiveWorkInstalledProfile:
    return ObjectiveWorkInstalledProfile(
        installed_profile_id=profile_id,
        owner_agent_id=scope,
        owner_canonical_user_id=owner,
        profile_name=f"daily-{owner}",
        profile=ObjectiveWorkProfile(cadence_cron="0 9 * * *", budget=BUDGET),
        source_proposal_id="objwork_prop_1",
        proposal_digest="a" * 64,
        installed_by_task_id="task_1",
        acceptance_summary="Accepted in chat.",
    )


def _policy(objective_id: str, scope: str, owner: str, **fields: Any) -> ObjectiveWorkPolicy:
    return ObjectiveWorkPolicy(
        objective_id=objective_id,
        owner_agent_id=scope,
        owner_canonical_user_id=owner,
        profile_name=f"daily-{owner}",
        cadence_cron="0 9 * * *",
        budget=BUDGET,
        accepted_by_task_id="task_1",
        acceptance_summary="Accepted in chat.",
        **fields,
    )


def _old_instance(tmp_path: Path) -> Path:
    from storage.gateway_state_repository import create_gateway_state_repository

    path, config = new_instance(tmp_path, "alpha", TEAM)
    people = create_gateway_state_repository(config.state_storage_settings)
    for person, role, status in (("usr_ana", "owner", "active"), ("usr_gone", "member", "removed")):
        people.create_gateway_user(canonical_user_id=person, display_name=person, timezone="UTC")
        people.create_team_membership(canonical_user_id=person, team_id=TEAM, role=role, status=status)
    scope = owner_scope(config)
    objectives = create_objective_repository(config.manager_storage_settings)
    for objective in (
        _objective("obj_ana", scope, "usr_ana"),
        _objective("obj_team", scope, None, ObjectiveStatus.PAUSED),
        _objective("obj_gone", scope, "usr_gone"),
        _objective("obj_done", scope, "usr_ana", ObjectiveStatus.SATISFIED),
        _objective("obj_deleting", scope, "usr_ana", ObjectiveStatus.DELETING),
        _objective("obj_incarnation", "agent_1234", "usr_ana"),
    ):
        objectives.save_objective(objective)
    work = create_objective_work_repository(config.manager_storage_settings)
    work.save_installed_profile(_profile("prof_ana", scope, "usr_ana"))
    work.save_installed_profile(_profile("prof_gone", scope, "usr_gone"))
    work.save_policy(
        _policy("obj_ana", scope, "usr_ana", installed_profile_id="prof_ana", status=ObjectiveWorkPolicyStatus.PAUSED)
    )
    work.save_policy(_policy("obj_gone", scope, "usr_gone"))
    return path


def test_open_objectives_and_their_work_setup_arrive_for_imported_people(tmp_path: Path) -> None:
    from assistant_gateway.config import GatewayInstanceConfig

    out = tmp_path / "team.uatransfer"
    export_team(
        export_source(_old_instance(tmp_path)), out, PASSPHRASE, owner_person_id="usr_ana", sections=SECTIONS,
        key_derivation=new_key_derivation(**FAST_KEYS),
    )
    target = tmp_path / "new"
    with TransferReader.open(out, PASSPHRASE) as reader:
        plan = import_team(reader, target, dry_run=False, sections=SECTIONS)

    outcomes = {r.key: r for r in plan.records if r.section == "objectives"}
    imported = {key for key, r in outcomes.items() if r.outcome is RecordOutcome.IMPORT}
    assert imported == {"objective/obj_ana", "objective/obj_team", "work_profile/prof_ana", "work_policy/obj_ana"}
    assert "objective/obj_done" not in outcomes, "closed objectives are history"
    assert "usr_gone, who is not imported" in outcomes["objective/obj_gone"].reason
    assert "usr_gone, who is not imported" in outcomes["work_profile/prof_gone"].reason
    assert "objective obj_gone is not imported" in outcomes["work_policy/obj_gone"].reason
    assert "it was deleting" in outcomes["objective/obj_deleting"].reason
    assert "agent_1234" in outcomes["objective/obj_incarnation"].reason

    config = GatewayInstanceConfig.load(target / "instance_config.yaml")
    scope = owner_scope(config)
    objectives = create_objective_repository(config.manager_storage_settings)
    arrived = {o.objective_id: o for o in objectives.list_team_objectives(team_id=TEAM)}
    assert set(arrived) == {"obj_ana", "obj_team"}
    assert arrived["obj_team"].status is ObjectiveStatus.PAUSED and arrived["obj_team"].owner_canonical_user_id is None
    assert {o.owner_agent_id for o in arrived.values()} == {scope}
    work = create_objective_work_repository(config.manager_storage_settings)
    policy = work.get_policy("obj_ana", owner_agent_id=scope)
    assert policy is not None and policy.status is ObjectiveWorkPolicyStatus.PAUSED
    assert policy.installed_profile_id == "prof_ana"
    assert work.get_policy("obj_gone", owner_agent_id=scope) is None
    profiles = work.list_installed_profiles(owner_agent_id=scope, owner_canonical_user_id="usr_ana")
    assert [p.installed_profile_id for p in profiles] == ["prof_ana"]
