"""Startup finds approvals saved in the removed permission format; approve-existing rewrites them.

Old configuration names decision and challenge ids from the removed permission
store: MCP connection records (schema version 1, whose approval named the old
kinds) and saved scheduled-worker settings. MCP attachment records were
retired in access plan 7d (grants on mcp:<name> decide use), so old attachment
files are ignored rather than re-approved.
``old_approval_records`` finds them, startup refuses with a message that names
``assistant_cli access approve-existing``, and ``reapprove`` records the
administrator as the approver so the current models load them; MCP servers the
registry first recorded from the operator's configuration keep that origin.
Records already in the current format are left alone.

Capability attachments in TEAM_AGENTS.yaml and generated specialists that name
capability ids were removed (access plan Phase 7). They are not re-approved:
startup refuses the roster, and the specialist's records no longer load.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from assistant_gateway.config import GatewayInstanceConfig
from custom_agents.generated_agent_runtime import GeneratedAgentProvisioningRecordV1
from custom_agents.team_composition import TEAM_COMPOSITION_SCHEMA_VERSION
from custom_messages.contracts.access_v2 import ChangeApprovalMode, ChangeApprovalRecordV1
from custom_messages.contracts.common_v1 import contract_sha256
from docker.launch_headless import RuntimeLayoutError, validate_runtime_layout
from mcp_control.registry import (
    OPERATOR_CONFIGURATION_APPROVER,
    MCPConnectionLifecycle,
    MCPConnectionRecordV1,
    MCPConnectionSource,
    MCPConnectionStore,
    _new_record,
    descriptor_from_server_config,
    resolve_mcp_connection_paths,
)
from runtime_settings.approval_cutover import (
    OLD_APPROVAL_RECORDS_PRESENT,
    old_approval_message,
    old_approval_records,
    reapprove,
)

TEAM = "team_cutover"
CHANGE_ID = "generated_agent_0123456789ab"
NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


def _instance(root: Path) -> GatewayInstanceConfig:
    config = GatewayInstanceConfig.create(instance_name="cutover", instance_root=str(root), team_id=TEAM, timezone="UTC")
    config.save(root / "instance_config.yaml")
    config.layout.control_dir.mkdir(parents=True, exist_ok=True)
    return config


def test_startup_refuses_a_roster_that_still_has_capability_attachments(tmp_path: Path) -> None:
    config = _instance(tmp_path / "instance")
    config.layout.team_composition_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": TEAM_COMPOSITION_SCHEMA_VERSION,
                "approved_attachments": {
                    "agent_types": {"CodeWritingAgent": [{"capability_id": "builtin:learning_coaching"}]}
                },
            }
        ),
        encoding="utf-8",
    )

    assert not old_approval_records(config.instance_root_path).present, "attachments are not re-approved"
    with pytest.raises(RuntimeLayoutError) as refused:
        validate_runtime_layout(config)

    assert "approved_attachments" in str(refused.value)
    assert "grants" in str(refused.value)


def test_generated_specialist_records_that_name_capabilities_are_not_reapproved(tmp_path: Path) -> None:
    """A specialist created before default tools is re-created, not rewritten."""

    config = _instance(tmp_path / "instance")
    old_record = {
        "schema_version": 2,
        "record_type": "generated_agent_provisioning",
        "provisioning_id": "gen_agent_prov_0123456789ab",
        "change_id": CHANGE_ID,
        "plan_digest": "2" * 64,
        "status": "ready",
        "agent_type": "ReportAgent",
        "template_name": "TaskSpecialistTemplate",
        "team_id": TEAM,
        "owner_canonical_user_id": "user_old",
        "actor_principal_id": "agent_team_lead",
        "capabilities": [{"capability_id": "builtin:web_research", "descriptor_sha256": "3" * 64}],
        "catalog_sha256": "4" * 64,
        "approval": {"mode": "agreement", "approved_by": "agreement", "approved_at_utc": "2026-09-01T09:00:00+00:00"},
        "created_at_utc": "2026-07-21T12:00:00Z",
        "applied_at_utc": "2026-07-21T12:00:01Z",
    }
    record_path = config.layout.control_dir / "agent_bundles" / "team_lead_generated" / "ReportAgent" / "provisioning.json"
    record_path.parent.mkdir(parents=True)
    record_path.write_text(json.dumps(old_record), encoding="utf-8")

    assert not old_approval_records(config.instance_root_path).present
    with pytest.raises(ValidationError):
        GeneratedAgentProvisioningRecordV1.model_validate(old_record)


# -- MCP connection and attachment records (Phase 4c-3) --------------------------------

OLD_STATIC_APPROVAL = {
    "kind": "grandfathered_upgrade_v1",
    "approved_at_utc": "2026-06-01T08:00:00+00:00",
    "approved_by_principal_id": "system:mcp_static_upgrade",
    "authorization_engine": "static_baseline",
}
OLD_PERSON_APPROVAL = {
    "kind": "authenticated_user",
    "approved_at_utc": "2026-07-02T10:00:00+00:00",
    "approved_by_principal_id": "user_old",
    "source_decision_id": "permission_decision_old",
    "authorization_engine": "cedar",
}
OLD_MANAGER_APPROVAL = {
    "kind": "authorized_manager",
    "approved_at_utc": "2026-07-03T10:00:00+00:00",
    "approved_by_principal_id": "agent_team_lead",
    "authorization_engine": "cedar",
}


def _descriptor(name: str, source: MCPConnectionSource, locator: str):
    return descriptor_from_server_config(
        {"name": name, "transport": "streamable_http", "server_url": f"https://{name}.example.test/mcp"},
        source=source,
        source_locator=locator,
        team_id=TEAM,
        owner_canonical_user_id="user_old",
    )


def _as_old(payload: dict, approval: dict | None, digest_field: str) -> dict:
    """A record as schema version 1 saved it: the old approval kinds and their digest."""

    old = {**payload, "schema_version": 1}
    old.pop("approval", None)
    if approval is not None:
        old["approval"] = approval
    old[digest_field] = ""
    excluded = {digest_field, "updated_at_utc"}
    old[digest_field] = contract_sha256({key: value for key, value in old.items() if key not in excluded})
    return old


def _write_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _old_mcp_registry(root: Path) -> dict[str, Path]:
    """An MCP registry saved before approvals were recorded as ChangeApprovalRecordV1.

    A server from the operator's configuration (grandfathered at the first
    upgrade), an agent's proposal a person approved, a pending proposal, their
    attachments, and an attachment a person detached earlier.
    """

    paths = resolve_mcp_connection_paths(root)
    placeholder = ChangeApprovalRecordV1(mode=ChangeApprovalMode.AGREEMENT, approved_by="x", approved_at_utc=NOW)
    written: dict[str, Path] = {}
    for key, descriptor, lifecycle, targets, approval in (
        (
            "static",
            _descriptor("configured", MCPConnectionSource.STATIC_GLOBAL, "agent.external_mcp_servers.configured"),
            MCPConnectionLifecycle.ACTIVE,
            ("*",),
            OLD_STATIC_APPROVAL,
        ),
        (
            "proposal",
            _descriptor("proposed", MCPConnectionSource.AGENT_PROPOSAL, "agent-proposal:agent_team_lead:1"),
            MCPConnectionLifecycle.ACTIVE,
            ("TeamArchitect", "WebResearchAgent"),
            OLD_PERSON_APPROVAL,
        ),
        (
            "pending",
            _descriptor("waiting", MCPConnectionSource.AGENT_PROPOSAL, "agent-proposal:agent_team_lead:2"),
            MCPConnectionLifecycle.PENDING,
            ("TeamArchitect",),
            None,
        ),
    ):
        current = _new_record(
            descriptor,
            lifecycle=lifecycle,
            target_agent_types=targets,
            approval=placeholder if approval is not None else None,
            now=NOW,
        )
        payload = current.model_dump(mode="json", exclude_none=True)
        written[key] = _write_json(
            paths.records / f"{current.connection_id}.json", _as_old(payload, approval, "record_digest")
        )
    # A retired attachment record from an old registry is neither read nor rewritten.
    written["retired_attachment"] = _write_json(
        paths.root / "attachments" / "mcp_attach_0123456789abcdef0123.json",
        {"schema_version": 1, "attachment_id": "mcp_attach_0123456789abcdef0123", "approval": OLD_MANAGER_APPROVAL},
    )
    return written


def test_old_mcp_records_are_found_named_and_refused_at_startup(tmp_path: Path) -> None:
    root = tmp_path / "instance"
    config = _instance(root)
    written = _old_mcp_registry(root)

    found = old_approval_records(root)
    message = old_approval_message(found, config_path=root / "instance_config.yaml")

    assert found.present
    assert sorted(found.mcp_connections) == sorted([written["static"], written["proposal"], written["pending"]])
    assert "- 3 MCP connection record(s)" in message and "attachment" not in message
    assert "MCP servers from the configuration file keep that origin" in message
    assert "assistant_cli.py access approve-existing --config" in message
    with pytest.raises(ValidationError):
        MCPConnectionRecordV1.model_validate_json(written["proposal"].read_text(encoding="utf-8"))
    with pytest.raises(RuntimeLayoutError) as refused:
        validate_runtime_layout(config)
    assert OLD_APPROVAL_RECORDS_PRESENT in str(refused.value)
    assert "3 MCP connection record(s)" in str(refused.value)


def test_approve_existing_rewrites_mcp_records_and_keeps_the_operators_configuration(tmp_path: Path) -> None:
    root = tmp_path / "instance"
    written = _old_mcp_registry(root)
    found = old_approval_records(root)

    changed = reapprove(found, person_id="user_admin", now=NOW)
    retired = written.pop("retired_attachment")

    assert sorted(changed) == sorted(f"MCP registry record {path.name}" for path in written.values())
    assert json.loads(retired.read_text(encoding="utf-8"))["schema_version"] == 1, "retired files are left alone"
    assert not old_approval_records(root).present
    store = MCPConnectionStore(resolve_mcp_connection_paths(root))
    records = {record.descriptor.connection_name: record for record in store.list_records()}
    assert {record.schema_version for record in records.values()} == {2}

    configured = records["configured"]
    assert configured.from_operator_configuration is True
    assert configured.approval == ChangeApprovalRecordV1(
        mode=ChangeApprovalMode.OPERATOR_CONFIGURATION,
        approved_by=OPERATOR_CONFIGURATION_APPROVER,
        approved_at_utc=datetime(2026, 6, 1, 8, 0, tzinfo=timezone.utc),
    ), "a server from the configuration file keeps that origin and its time"

    proposed = records["proposed"]
    assert proposed.lifecycle == MCPConnectionLifecycle.ACTIVE
    assert proposed.approval == ChangeApprovalRecordV1(
        mode=ChangeApprovalMode.PERSON_APPROVAL, approved_by="user_admin", approved_at_utc=NOW
    )

    waiting = records["waiting"]
    assert (waiting.lifecycle, waiting.approval) == (MCPConnectionLifecycle.PENDING, None)
    # Digests were recomputed: re-reading validates them, and a second run changes nothing.
    assert reapprove(old_approval_records(root), person_id="user_admin", now=NOW) == []


def test_a_current_mcp_registry_needs_nothing(tmp_path: Path) -> None:
    root = tmp_path / "instance"
    store = MCPConnectionStore(resolve_mcp_connection_paths(root))
    approval = ChangeApprovalRecordV1(
        mode=ChangeApprovalMode.PERSON_APPROVAL, approved_by="user_admin", approved_at_utc=NOW, access_request_id="r1"
    )
    store.save(
        _new_record(
            _descriptor("current", MCPConnectionSource.AGENT_PROPOSAL, "agent-proposal:agent_team_lead:3"),
            lifecycle=MCPConnectionLifecycle.ACTIVE,
            target_agent_types=("TeamArchitect",),
            approval=approval,
            now=NOW,
        )
    )

    assert not old_approval_records(root).present
