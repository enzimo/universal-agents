"""The execution control service under access model 2, with real Cedar and SQLite.

Promoting revisions, creating and enabling jobs, and creating SSH sessions are
``write`` operations of ``fn:execution_control`` decided by the access decision
hook. The service records who held access in each activation (agent, verified
person and roles, catalog operation); with authorization off it records the
person's agreement instead. Runs and SSH use are covered in
``test_execution_runner.py`` and ``test_execution_ssh.py``; managed services in
``test_execution_services.py``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import assistant_cli
from custom_agents.access_runtime import decide_call
from custom_agents.execution_changes import ManagedServiceChangeService
from custom_agents.execution_control_service import ExecutionControlService
from custom_messages.contracts.access_v2 import AccessLevel, ChangeApprovalMode, GrantLifetime
from tests.task_work_support import set_agent_ref
from runtime_hosting.controller_service import run_execution_controller
from runtime_hosting.execution_contracts import (
    AccessHolderV1,
    AutomationScheduleKind,
    AutomationScheduleV1,
    ExecutionActivationKind,
    OwnerScopeKind,
    OwnerScopeV1,
    ServiceKind,
    ServiceRestartPolicy,
)
from tests.change_approval_support import TASK_ID, needs_cedar, use_signed_contexts
from tests.execution_access_support import (
    LOCAL_PROFILE_ID,
    PERSON,
    SSH_TARGET_ID,
    TEAM,
    execution_instance,
    execution_lead,
    grant_execution,
    grant_unconfined,
)


@pytest.fixture(autouse=True)
def _signed_contexts(monkeypatch: pytest.MonkeyPatch):
    use_signed_contexts(monkeypatch)
    yield
    set_agent_ref(None)


def _one_shot() -> AutomationScheduleV1:
    return AutomationScheduleV1(
        schedule_kind=AutomationScheduleKind.ONE_SHOT,
        one_shot_at_utc=datetime.now(timezone.utc),
    )


def _revision_id(service: ExecutionControlService, profile_id: str = LOCAL_PROFILE_ID) -> str:
    return service.promote_revision(
        source="print('deterministic')\n",
        source_name="main.py",
        language="python",
        profile_id=profile_id,
    ).revision_id


@needs_cedar
def test_the_hook_decides_job_creation_and_the_activation_records_who_held_access(tmp_path: Path) -> None:
    agent = execution_lead(execution_instance(tmp_path, authorization_enabled=True))
    arguments = {"name": "nightly", "revision_id": "exec_rev_x", "activate": True}

    refused = decide_call(agent, "create_execution_job", arguments)
    assert refused is not None and not refused.allowed
    assert (refused.tool_id, refused.level) == ("fn:execution_control", AccessLevel.WRITE)

    grant_execution(agent)
    allowed = decide_call(agent, "create_execution_job", arguments)
    assert allowed is not None and allowed.allowed
    service = ExecutionControlService(agent)
    revision_id = _revision_id(service)
    # The test profile runs code on the controller host, outside any sandbox.
    with pytest.raises(PermissionError, match="code:unconfined"):
        service.create_job(name="nightly", revision_id=revision_id, schedule=_one_shot(), activate=True)
    grant_unconfined(agent)
    job = service.create_job(name="nightly", revision_id=revision_id, schedule=_one_shot(), activate=True)

    assert job.enabled is True
    assert job.owner == OwnerScopeV1(scope_kind=OwnerScopeKind.USER, owner_id=PERSON, team_id=TEAM)
    activation = job.activation
    assert activation is not None
    assert activation.kind is ExecutionActivationKind.RUN
    assert activation.operation == "fn:execution_control#create_execution_job"
    assert activation.holder == AccessHolderV1(
        agent_id="agent_team_lead",
        agent_type="TeamArchitect",
        person_id=PERSON,
        person_roles=("owner",),
        task_id=TASK_ID,
    )
    assert (activation.approval.mode, activation.approval.approved_by) == (
        ChangeApprovalMode.HELD_ACCESS,
        "TeamArchitect",
    )
    assert activation.resource_digest == job.activation_resource_digest()
    assert service.list_jobs() == (job,)


@needs_cedar
def test_a_job_needs_access_that_lasts_beyond_the_task(tmp_path: Path) -> None:
    agent = execution_lead(execution_instance(tmp_path, authorization_enabled=True))
    # Access for one task is bound to the request the task works on (its signed context).
    from custom_agents.access_runtime import verified_context_id

    grant_execution(agent, lifetime=GrantLifetime.TASK, task_id=verified_context_id(agent))
    decision = decide_call(agent, "create_execution_job", {})
    assert decision is not None and decision.allowed, "the call itself is allowed in this task"
    service = ExecutionControlService(agent)
    revision_id = _revision_id(service)

    with pytest.raises(PermissionError, match="lasts beyond this task"):
        service.create_job(name="later", revision_id=revision_id, schedule=_one_shot(), activate=True)

    draft = service.create_job(name="draft", revision_id=revision_id, schedule=_one_shot())
    assert (draft.enabled, draft.activation) == (False, None)


@needs_cedar
def test_enabling_a_job_records_held_access_without_agreement(tmp_path: Path) -> None:
    agent = execution_lead(execution_instance(tmp_path, authorization_enabled=True))
    grant_execution(agent)
    grant_unconfined(agent)
    service = ExecutionControlService(agent)
    job = service.create_job(name="toggle", revision_id=_revision_id(service), schedule=_one_shot(), activate=True)

    disabled = service.set_job_enabled(job_id=job.job_id, enabled=False)
    enabled = service.set_job_enabled(job_id=job.job_id, enabled=True)

    assert (disabled.enabled, disabled.activation) == (False, None)
    assert enabled.activation is not None
    assert enabled.activation.operation == "fn:execution_control#set_execution_job_enabled"
    assert enabled.activation.generation == enabled.generation == job.generation + 2
    assert enabled.activation.approval.mode is ChangeApprovalMode.HELD_ACCESS


def test_agreement_is_the_authority_with_authorization_off(tmp_path: Path) -> None:
    agent = execution_lead(execution_instance(tmp_path, authorization_enabled=False))
    service = ExecutionControlService(agent)
    revision_id = _revision_id(service)

    with pytest.raises(PermissionError, match="confirmed=true"):
        service.create_job(name="denied", revision_id=revision_id, schedule=_one_shot(), activate=True)
    job = service.create_job(
        name="agreed", revision_id=revision_id, schedule=_one_shot(), activate=True, agreement_confirmed=True
    )
    assert job.activation is not None
    assert (job.activation.approval.mode, job.activation.approval.approved_by) == (
        ChangeApprovalMode.AGREEMENT,
        "agreement",
    )
    with pytest.raises(PermissionError, match="confirmed=true"):
        service.set_job_enabled(job_id=job.job_id, enabled=False)

    disabled = service.set_job_enabled(job_id=job.job_id, enabled=False, agreement_confirmed=True)
    enabled = service.set_job_enabled(job_id=job.job_id, enabled=True, agreement_confirmed=True)

    assert disabled.activation is None
    assert enabled.activation is not None
    assert enabled.activation.resource_digest == enabled.activation_resource_digest()
    assert enabled.activation.approval.mode is ChangeApprovalMode.AGREEMENT


def test_ssh_sessions_need_agreement_with_authorization_off(tmp_path: Path) -> None:
    agent = execution_lead(execution_instance(tmp_path, authorization_enabled=False))
    service = ExecutionControlService(agent)

    with pytest.raises(PermissionError, match="confirmed=true"):
        service.create_ssh_session(target_id=SSH_TARGET_ID)
    session = service.create_ssh_session(target_id=SSH_TARGET_ID, agreement_confirmed=True)

    assert session.connect_activation is not None
    assert session.connect_activation.approval.mode is ChangeApprovalMode.AGREEMENT


def test_service_rejects_credential_like_environment_values(tmp_path: Path) -> None:
    service = ExecutionControlService(execution_lead(execution_instance(tmp_path, authorization_enabled=False)))

    with pytest.raises(ValueError, match="credential material"):
        service.promote_revision(
            source="print('safe')\n",
            source_name="main.py",
            language="python",
            profile_id=LOCAL_PROFILE_ID,
            non_secret_env={"API_TOKEN": "not-allowed"},
        )


def test_managed_services_need_a_kubernetes_profile(tmp_path: Path) -> None:
    agent = execution_lead(execution_instance(tmp_path, authorization_enabled=False))
    revision_id = _revision_id(ExecutionControlService(agent))

    with pytest.raises(ValueError, match="Kubernetes execution profile"):
        ManagedServiceChangeService(agent).stage_create(
            name="local",
            revision_id=revision_id,
            service_kind=ServiceKind.PROCESS,
            restart_policy=ServiceRestartPolicy.ALWAYS,
            reason="Run the local profile as a service",
        )


def test_old_records_stop_the_controller_until_the_operator_retires_them(tmp_path: Path, capsys) -> None:
    instance = execution_instance(tmp_path, authorization_enabled=False)
    service = ExecutionControlService(execution_lead(instance))
    job = service.create_job(
        name="old", revision_id=_revision_id(service), schedule=_one_shot(), activate=True, agreement_confirmed=True
    )
    with service.repository._connect() as connection:  # type: ignore[attr-defined]
        row = connection.execute(
            "SELECT record_json FROM execution_automation_jobs WHERE job_id = ?", (job.job_id,)
        ).fetchone()
        old = json.loads(row["record_json"])
        old["schema_version"] = 1
        old["activation"] = {**old["activation"], "authority_mode": "authenticated_authorization"}
        connection.execute(
            "UPDATE execution_automation_jobs SET record_json = ? WHERE job_id = ?", (json.dumps(old), job.job_id)
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="execution-retire-old-records"):
        asyncio.run(run_execution_controller(instance.config))
    assistant_cli._execution_retire_old_records(argparse.Namespace(config=str(instance.config_path), env=None))

    assert f"Retired {job.job_id}" in capsys.readouterr().out
    retired = service.repository.get_job(team_id=TEAM, job_id=job.job_id)
    assert retired is not None and (retired.enabled, retired.activation) == (False, None)
    assert service.repository.old_format_records(team_id=TEAM).present is False
