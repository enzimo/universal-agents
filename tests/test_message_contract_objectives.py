"""Contract conformance against persisted Objective coordinator records."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import rfc8785
from jsonschema import ValidationError as SchemaValidationError

from custom_agents.objective_work_sessions import _proposal_digest
from custom_messages.enums import ObjectiveHealthDisposition, RequesterType
from custom_messages.records.objectives import objective_version
from custom_messages.wire.encoding import b64url
from custom_messages.wire.frame import FrameRefused, format_timestamp, sign_frame
from custom_messages.wire.kinds import MAX_FRAME_BYTES
from custom_tools.providers.objective_work import (
    objective_work_profiles,
    objective_work_sessions,
    propose_objective_work_profile,
)
from custom_tools.providers.objectives import create_objective, record_objective_work_checkpoint
from custom_tools.runtime_context import set_agent_ref
from tests.change_approval_support import use_signed_contexts
from tests import message_contract_support as fixtures
from tests.test_message_contract import EXPORT, checked, validator
from tests.test_objective_work_sessions import _manager, _objective, _tool_agent
from tests.tool_result_support import tool_json


def record_payload(value: Any, schema_file: str, schema: dict[str, Any]) -> Any:
    """Map real records, refusing any field the wire cannot preserve.

    Only the documented name/unit changes, text references and timestamp
    normalization happen here. The dispatcher remains future work.
    """
    schemas = EXPORT.load_schemas()
    resolver = EXPORT.ModelWriter(schemas)

    def mapped(value: Any, file: str, node: dict[str, Any]) -> Any:
        while "$ref" in node:
            file, fragment = resolver.resolve(file, node["$ref"])
            node = resolver.node(file, fragment)
        if "oneOf" in node:
            for item in node["oneOf"]:
                if item.get("properties", {}).get("type", {}).get("const") == "inline":
                    slot = "text" if "text" in item["properties"] else "value"
                    return {"type": "inline", slot: mapped(value, file, item["properties"][slot])}
            # Catalog entries keep the runtime's configured/installed discriminator.
            variant = next(item for item in node["oneOf"] if any(
                prop.get("const") == value.get(key) for key, prop in item["properties"].items() if "const" in prop
            ))
            return mapped(value, file, variant)
        if node.get("type") == "array":
            return [mapped(item, file, node["items"]) for item in value]
        if node.get("type") == "object":
            result = {}
            for key, item in value.items():
                if item is None or key == "message_class":
                    continue
                renamed = {"owner_canonical_user_id": "owner_person", "active_seconds_used": "active_ms_used",
                           "details": "details_json", "tuning_signals": "tuning_signals_json"}.get(key, key)
                if renamed == "active_ms_used":
                    item = int(item * 1000)
                if renamed in {"details_json", "tuning_signals_json"}:
                    item = json.dumps(item)
                assert renamed in node["properties"], f"Lost runtime field {key} in {file}"
                result[renamed] = mapped(item, file, node["properties"][renamed])
            return result
        if node.get("pattern") == schemas["common.schema.json"]["$defs"]["timestamp"]["pattern"]:
            return format_timestamp(datetime.fromisoformat(value))
        return value

    return mapped(value, schema_file, schema)


def test_real_objective_work_records_preserve_durable_manager_owners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_signed_contexts(monkeypatch)
    manager = _manager(tmp_path, policy=True)
    task, objective = _objective(manager)
    coordinator = manager.objective_work_coordinator
    policy = coordinator.enable_policy(objective.objective_id, profile_name="daily", accepted_by_task_id=task.task_id,
                                       acceptance_summary="Run daily bounded sessions.")
    session = coordinator.start_session(objective.objective_id, task_id=task.task_id)
    checkpoint = coordinator.checkpoint(
        session.session_id, observe="Inspected the change.", orient="The goal is parity.", decide="Verify the records.",
        act="Ran contract checks.", health=ObjectiveHealthDisposition.ON_TRACK, evidence_delta=["SQLite record saved."],
    )
    schemas = EXPORT.load_schemas()
    for kind, field, record in [
        ("objectives.work_sessions.enable", "policy", policy),
        ("objectives.work_sessions.start", "session", session),
        ("objectives.record_checkpoint", "checkpoint", checkpoint),
        ("objectives.get", "objective", manager.get_objective(objective.objective_id)),
    ]:
        file = f"bodies/{kind}.reply.schema.json"
        node = schemas[file]["oneOf"][0]["properties"][field]
        payload = record_payload(record.model_dump(mode="json"), file, node)
        assert payload["value"]["owner_agent_id"] == policy.owner_agent_id
        assert policy.owner_agent_id.startswith("manager_scope:")
        body = {"outcome": "ok", field: payload}
        if kind == "objectives.get":
            body.update(objective_id=objective.objective_id, seen_version=objective_version(record),
                        markdown={"type": "inline", "text": manager.render_objective_markdown(objective.objective_id)})
        checked(kind + ".reply", body)


def test_durable_owner_ids_preserve_long_scopes_without_weakening_routing_ids() -> None:
    owner = "manager_scope:" + "t" * 64 + ":" + "r" * 64
    assert len(owner) == 143
    validator("common.schema.json#/$defs/durable_owner_id").validate(owner)
    for definition in ("agent_id", "record_id"):
        with pytest.raises(SchemaValidationError):
            validator(f"common.schema.json#/$defs/{definition}").validate(owner)


def _checked_record(kind: str, field: str, record: Any, extra: dict[str, Any] | None = None) -> Any:
    file = f"bodies/{kind}.reply.schema.json"
    node = EXPORT.load_schemas()[file]["oneOf"][0]["properties"][field]
    payload = record_payload(record, file, node)
    checked(kind + ".reply", {"outcome": "ok", field: payload, **(extra or {})})
    return payload["value"]


def verified_record_file(
    tmp_path: Path, kind: str, field: str, record: dict[str, Any], extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Verify a large record's file contract using local bytes, before future blob dispatch."""
    inline = {"outcome": "ok", field: {"type": "inline", "value": record}, **(extra or {})}
    checked(kind + ".reply", inline)
    frame = fixtures.example_frame(kind + ".reply", "ok")
    key = fixtures.private_key(frame["issuer"]["key_id"])
    frame["body"] = inline
    signed = sign_frame(frame, key)
    data = rfc8785.dumps(signed)
    assert len(data) > MAX_FRAME_BYTES
    with pytest.raises(FrameRefused, match="too_large"):
        fixtures.verify(data, signed)

    path = tmp_path / (kind + ".json")
    path.write_bytes(rfc8785.dumps(record))
    data = path.read_bytes()
    reference = {"sha256": b64url(hashlib.sha256(data).digest()), "size": len(data), "media_type": "application/json"}
    frame["body"] = {**inline, field: {"type": "blob", "ref": reference}}
    checked(kind + ".reply", frame["body"])
    signed = sign_frame(frame, key)
    data = rfc8785.dumps(signed)
    assert len(data) < MAX_FRAME_BYTES
    fixtures.verify(data, signed)

    restored = json.loads(path.read_bytes())
    assert isinstance(restored, dict)
    checked(kind + ".reply", {**inline, field: {"type": "inline", "value": restored}})
    assert restored == record
    return restored


def test_large_stored_checkpoint_collections_survive_reply_and_status_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_signed_contexts(monkeypatch)
    manager = _manager(tmp_path, policy=True)
    task, objective = _objective(manager)
    _tool_agent(manager, task.task_id, monkeypatch)
    coordinator = manager.objective_work_coordinator
    assert coordinator.objective_work_config is not None
    coordinator.objective_work_config.visibility.emit_activity_snapshots = False
    coordinator.enable_policy(objective.objective_id, profile_name="daily", accepted_by_task_id=task.task_id,
                              acceptance_summary="Keep complete checkpoints.", owner_canonical_user_id="objective-user")
    session = coordinator.start_session(objective.objective_id, task_id=task.task_id,
                                        owner_canonical_user_id="objective-user")
    current = manager.get_task_info(task.task_id)
    assert current is not None
    current.meta.objective_work_session_id = session.session_id
    collections = {name: [f"{name}-{index}-" + "x" * 80 for index in range(1001)] for name in (
        "evidence_delta", "completed_subgoals", "blockers", "in_flight_effects", "artifacts",
    )}
    try:
        checkpoint = tool_json(record_objective_work_checkpoint(
            observe="Read stored work.", orient="Preserve every item.", decide="Verify the file contract.",
            act="Read status.", health="on_track", **{name + "_json": json.dumps(items)
                                                      for name, items in collections.items()},
        ))
        assert "error" not in checkpoint, checkpoint
        snapshot = tool_json(objective_work_sessions(action="status", objective_id=objective.objective_id))
        for kind, field, raw in [
            ("objectives.record_checkpoint", "checkpoint", checkpoint),
            ("objectives.work_sessions.status", "snapshot", snapshot),
        ]:
            file = f"bodies/{kind}.reply.schema.json"
            node = EXPORT.load_schemas()[file]["oneOf"][0]["properties"][field]
            payload = record_payload(raw, file, node)["value"]
            restored = verified_record_file(tmp_path, kind, field, payload)
            stored = restored if field == "checkpoint" else restored["checkpoints"][0]
            for name, items in collections.items():
                assert [item["text"] for item in stored[name]] == items
    finally:
        set_agent_ref(None)


def test_complete_objective_files_keep_existing_large_criteria_and_constraints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_signed_contexts(monkeypatch)
    manager = _manager(tmp_path, policy=True)
    task, _ = _objective(manager)
    _tool_agent(manager, task.task_id, monkeypatch)
    # Existing stores accept these collections even when new wire writes have a lower count bound.
    items = [f"Stored requirement {index}: " + "x" * 100 for index in range(1001)]
    try:
        result = tool_json(create_objective(
            title="Read complete existing records", desired_outcome="Keep the saved collections.",
            why="A read must preserve the complete stored value.", task_id=task.task_id, creation_mode="spawn_task",
            success_criteria_json=json.dumps(items), constraints_json=json.dumps(items), non_goals_json=json.dumps(items),
        ))
        assert "error" not in result, result
        saved = manager.get_objective(result["objective"]["objective_id"])
        assert saved is not None
        file = "bodies/objectives.get.reply.schema.json"
        node = EXPORT.load_schemas()[file]["oneOf"][0]["properties"]["objective"]
        payload = record_payload(saved.model_dump(mode="json"), file, node)["value"]
        markdown = manager.render_objective_markdown(saved.objective_id).encode()
        extra = {"objective_id": saved.objective_id, "seen_version": objective_version(saved), "markdown": {
            "type": "blob", "ref": {"sha256": b64url(hashlib.sha256(markdown).digest()), "size": len(markdown),
                                     "media_type": "text/plain"},
        }}
        restored = verified_record_file(tmp_path, "objectives.get", "objective", payload, extra)
        assert len(restored["success_criteria"]) == 1001
        assert [item["description"]["text"] for item in restored["success_criteria"]] == items
        for name in ("constraints", "non_goals"):
            assert [item["text"] for item in restored[name]] == items
    finally:
        set_agent_ref(None)


def test_creation_preserves_structured_criteria_and_runtime_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_signed_contexts(monkeypatch)
    manager = _manager(tmp_path, policy=True)
    task, _ = _objective(manager)
    _tool_agent(manager, task.task_id, monkeypatch)
    criteria: list[dict[str, Any]] = [
        {"type": "criterion", "criterion_id": "criterion-tools", "status": "met",
         "description": {"type": "inline", "text": "The tool path is covered."},
         "evidence_summary": {"type": "inline", "text": "SQLite checks passed."},
         "updated_at": "2026-10-20T01:02:03.000Z"},
        {"type": "inline", "text": "The report is complete."},
        {"type": "criterion", "description": {"type": "inline", "text": "A reviewer accepts the report."}},
    ]
    body: dict[str, Any] = {"request_id": "request-create-criteria", "title": "Verify contract parity",
            "desired_outcome": {"type": "inline", "text": "Preserve the saved criteria."},
            "why": {"type": "inline", "text": "Creation must retain supplied evidence."},
            "horizon": "finite", "priority": "normal", "task_id": task.task_id,
            "creation_mode": "spawn_task", "success_criteria": criteria}
    checked("objectives.create", body)
    # This is the documented phase-4 field mapping, exercised against today's public tool.
    runtime_criteria: list[Any] = []
    for criterion in criteria:
        if criterion["type"] == "inline":
            runtime_criteria.append(criterion["text"])
        else:
            runtime_criteria.append({
                key: value["text"] if isinstance(value, dict) else value
                for key, value in criterion.items() if key != "type"
            })
    try:
        result = tool_json(create_objective(
            title=body["title"], desired_outcome=body["desired_outcome"]["text"],
            why=body["why"]["text"], task_id=task.task_id, creation_mode="spawn_task",
            success_criteria_json=json.dumps(runtime_criteria),
        ))
    finally:
        set_agent_ref(None)
    assert "error" not in result, result
    saved = manager.get_objective(result["objective"]["objective_id"])
    assert saved is not None
    first, text_only, defaulted = saved.success_criteria
    assert (first.criterion_id, first.status.value, first.evidence_summary) == (
        "criterion-tools", "met", "SQLite checks passed.",
    )
    assert datetime.fromisoformat(first.updated_at) == datetime.fromisoformat(criteria[0]["updated_at"])
    assert [item.status.value for item in (text_only, defaulted)] == ["open", "open"]
    assert text_only.description == "The report is complete."
    assert len({first.criterion_id, text_only.criterion_id, defaulted.criterion_id}) == 3
    for item in (text_only, defaulted):
        assert item.criterion_id and item.updated_at and item.evidence_summary is None
    _checked_record("objectives.get", "objective", saved.model_dump(mode="json"), {
        "objective_id": saved.objective_id, "seen_version": objective_version(saved),
        "markdown": {"type": "inline", "text": manager.render_objective_markdown(saved.objective_id)},
    })


def test_profile_propose_catalog_remove_keep_persisted_records_and_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_signed_contexts(monkeypatch)
    manager = _manager(tmp_path, policy=True)
    task, objective = _objective(manager)
    _tool_agent(manager, task.task_id, monkeypatch)
    coordinator = manager.objective_work_coordinator
    try:
        proposal = tool_json(propose_objective_work_profile(
            profile_name="six-hour-focus", cadence_cron="0 */6 * * *", active_seconds=2700,
            checkpoint_reserve_seconds=300, checkpoint_reserve_turns=2, checkpoint_reserve_tokens=50000,
            checkpoint_reserve_tool_calls=1, turns=18, total_tokens=250000, tool_calls=35,
            rationale="Use bounded runs for future Objectives.", scope="reusable",
        ))
        checked("objectives.work_profiles.propose", {
            "request_id": "request-propose", "task_id": task.task_id, "profile_name": proposal["profile_name"],
            "scope": "reusable", "profile": record_payload(
                proposal["profile"], "bodies/objectives.work_profiles.propose.schema.json",
                EXPORT.load_schemas()["bodies/objectives.work_profiles.propose.schema.json"]["properties"]["profile"],
            ), "rationale": {"type": "inline", "text": proposal["rationale"]},
        })
        _checked_record("objectives.work_profiles.propose", "proposal", proposal)
        assert proposal["status"] == "pending"
        catalog = tool_json(objective_work_profiles(action="list"))
        assert catalog["proposals"][0]["proposal_id"] == proposal["proposal_id"]
        _checked_record("objectives.work_profiles.list", "catalog", catalog)
        with pytest.raises(ValueError, match="Unknown"):
            coordinator.require_profile_proposal(proposal["proposal_id"], proposal_digest=proposal["proposal_digest"],
                                                 owner_canonical_user_id="another-person")
        with pytest.raises(PermissionError, match="does not match"):
            coordinator.require_profile_proposal(proposal["proposal_id"], proposal_digest="0" * 64,
                                                 owner_canonical_user_id="objective-user")
        apply_inputs: dict[str, Any] = {"action": "apply", "proposal_id": proposal["proposal_id"],
                        "proposal_digest": proposal["proposal_digest"], "confirmation_summary": "Install this exact profile."}
        denied = tool_json(objective_work_profiles(**apply_inputs, confirmed=False))
        assert "confirmation" in denied["error"]
        applied = tool_json(objective_work_profiles(**apply_inputs, confirmed=True))
        assert applied["installed_profile"]["status"] == "active"
        policy = coordinator.enable_policy(objective.objective_id, profile_name="six-hour-focus",
                                           accepted_by_task_id=task.task_id, acceptance_summary="Use the installed profile.",
                                           owner_canonical_user_id="objective-user")
        catalog = _checked_record("objectives.work_profiles.list", "catalog", tool_json(objective_work_profiles(action="list")))
        assert {item["profile_source"] for item in catalog["profiles"]} == {"configured", "installed"}
        removal: dict[str, Any] = {"action": "remove", "profile_name": "six-hour-focus", "confirmation_summary": "Remove the reusable profile."}
        assert "confirmation" in tool_json(objective_work_profiles(**removal, confirmed=False))["error"]
        removed = tool_json(objective_work_profiles(**removal, confirmed=True))
        wire_removed = _checked_record("objectives.work_profiles.remove", "installed_profile", removed)
        assert wire_removed["status"] == "removed"
        assert wire_removed["removed_by_task_id"] == task.task_id
        without_proposals = tool_json(objective_work_profiles(action="list", include_proposals=False))
        assert "proposals" not in without_proposals
        assert [item["profile_source"] for item in without_proposals["profiles"]] == ["configured"]
        _checked_record("objectives.work_profiles.list", "catalog", without_proposals)
        assert coordinator.snapshot(objective.objective_id, owner_canonical_user_id="objective-user")["policy"]["budget"] == policy.budget.model_dump(mode="json")
        current = manager.get_task_info(task.task_id)
        assert current is not None
        current.meta.requester_type = RequesterType.TIMER_REQUESTER
        # A valid signed person context does not turn a timer into a direct person request.
        assert tool_json(objective_work_profiles(**removal, confirmed=True))["error"] == "direct_user_request_required"
        assert tool_json(propose_objective_work_profile(
            profile_name="timer-profile", cadence_cron="0 */6 * * *", active_seconds=2700,
            checkpoint_reserve_seconds=300, checkpoint_reserve_turns=2, checkpoint_reserve_tokens=50000,
            checkpoint_reserve_tool_calls=1, turns=18, total_tokens=250000, tool_calls=35,
            rationale="A timer must not propose a profile.", scope="reusable",
        ))["error"] == "direct_user_request_required"
    finally:
        set_agent_ref(None)


def test_status_preserves_missing_policy_and_disabled_coordinator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_signed_contexts(monkeypatch)
    for enabled in (True, False):
        folder = tmp_path / str(enabled)
        folder.mkdir()
        manager = _manager(folder, policy=enabled)
        task, objective = _objective(manager)
        _tool_agent(manager, task.task_id, monkeypatch)
        try:
            raw = tool_json(objective_work_sessions(action="status", objective_id=objective.objective_id))
            snapshot = _checked_record("objectives.work_sessions.status", "snapshot", raw)
            expected: dict[str, Any] = {"sessions": [], "checkpoints": []} if enabled else {}
            assert snapshot == expected
        finally:
            set_agent_ref(None)


def test_session_status_keeps_history_bounds_fractional_signals_and_json_file_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_signed_contexts(monkeypatch)
    manager = _manager(tmp_path, policy=True)
    task, objective = _objective(manager)
    _tool_agent(manager, task.task_id, monkeypatch)
    coordinator = manager.objective_work_coordinator
    coordinator.objective_work_config.visibility.checkpoint_history_limit = 3
    assert manager.objective_work_repository is not None
    coordinator.enable_policy(objective.objective_id, profile_name="daily", accepted_by_task_id=task.task_id,
                              acceptance_summary="Use bounded sessions.", owner_canonical_user_id="objective-user")
    # Start and checkpoint real sessions so the database, ordering and history windows are exercised together.
    for index in range(22):
        session = coordinator.start_session(objective.objective_id, task_id=task.task_id,
                                            owner_canonical_user_id="objective-user")
        session.turns_used = 3
        session.total_tokens_used = 10
        session.input_tokens_used = 7
        session.output_tokens_used = 3
        session.active_seconds_used = 1.2345
        manager.objective_work_repository.save_session(session)
        coordinator.checkpoint(session.session_id, observe=f"Run {index}", orient="Verify parity.",
                               decide="Continue.", act="Saved a checkpoint.", health=ObjectiveHealthDisposition.ON_TRACK)
    try:
        raw = tool_json(objective_work_sessions(action="status", objective_id=objective.objective_id))
        snapshot = _checked_record("objectives.work_sessions.status", "snapshot", raw)
        assert len(snapshot["sessions"]) == 20
        assert len(snapshot["checkpoints"]) == 3
        assert snapshot["checkpoints"][0]["observe"]["text"] == "Run 21"
        assert snapshot["sessions"][0]["active_ms_used"] == 1234
        signals = json.loads(snapshot["tuning_signals_json"])
        assert signals == raw["tuning_signals"]
        assert signals["average_tokens_per_turn"] == 10 / 3
        assert signals["input_to_output_token_ratio"] == 7 / 3
        assert signals["objective_profile_tool_budget_is_authoritative"] is True
        # JSON files contain this exact typed value, not the inline/blob wrapper or truncated records.
        payload = json.loads(json.dumps(snapshot))
        validator("bodies/objectives.work_sessions.status.reply.schema.json#/$defs/work_session_snapshot").validate(payload)
        with pytest.raises(PermissionError):
            coordinator.snapshot(objective.objective_id, owner_canonical_user_id="another-person")
    finally:
        set_agent_ref(None)


def test_profile_catalog_retains_latest_fifty_proposals_and_rejects_expired_acceptance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_signed_contexts(monkeypatch)
    manager = _manager(tmp_path, policy=True)
    task, _ = _objective(manager)
    coordinator = manager.objective_work_coordinator
    assert coordinator.objective_work_config is not None
    assert manager.objective_work_repository is not None
    profile = coordinator.objective_work_config.profiles["daily"]
    proposals = [coordinator.propose_profile(
        owner_canonical_user_id="objective-user", source_task_id=task.task_id, scope="reusable", objective_id=None,
        profile_name=f"candidate-{index}", profile=profile, rationale="Compare bounded cadences.", replace_existing=False,
    ) for index in range(51)]
    foreign = coordinator.propose_profile(
        owner_canonical_user_id="another-person", source_task_id=task.task_id, scope="reusable", objective_id=None,
        profile_name="private-candidate", profile=profile, rationale="Another person's proposal.", replace_existing=False,
    )
    catalog = coordinator.profile_catalog(owner_canonical_user_id="objective-user")
    assert [item["proposal_id"] for item in catalog["proposals"]] == [item.proposal_id for item in reversed(proposals[1:])]
    assert foreign.proposal_id not in {item["proposal_id"] for item in catalog["proposals"]}
    _checked_record("objectives.work_profiles.list", "catalog", catalog)
    expired = proposals[-1].model_copy(update={"created_at": "2020-01-01T00:00:00+00:00", "expires_at": "2020-01-02T00:00:00+00:00"})
    expired.proposal_digest = _proposal_digest(expired)
    manager.objective_work_repository.save_profile_proposal(expired)
    _checked_record("objectives.work_profiles.propose", "proposal", expired.model_dump(mode="json"))
    with pytest.raises(PermissionError, match="expired"):
        coordinator.require_profile_proposal(expired.proposal_id, proposal_digest=expired.proposal_digest,
                                             owner_canonical_user_id="objective-user")
