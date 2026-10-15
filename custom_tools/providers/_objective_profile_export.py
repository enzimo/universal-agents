"""Deterministic YAML export for bounded Objective execution profiles."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict

import yaml

from custom_agents.runtime_paths import agent_layout
from custom_messages.enums import RequesterType
from custom_messages.records.objectives import ObjectiveWorkProfile
from custom_tools.providers._runtime import (
    _get_current_task_info,
    _require_manager_tool,
    _resolve_objective_id_for_tool,
)
from custom_tools.providers._user_scope import _private_user_scope
from custom_tools.providers.diagnostics import _coerce_tool_bool
from custom_tools.runtime_context import get_agent_ref

if TYPE_CHECKING:
    from custom_agents.strands_agent import UniversalAgent


def _resolve_profile_for_export(
    agent: "UniversalAgent",
    *,
    profile_name: str,
    objective_id: str,
    owner_canonical_user_id: str,
    task_id: str,
) -> tuple[str, ObjectiveWorkProfile, str]:
    coordinator = agent.task_manager.objective_work_coordinator
    resolved_objective_id = objective_id.strip()
    if not resolved_objective_id:
        selected_name, selected_profile, source, _installed_id, _proposal_id = (
            agent.task_manager.operations_runtime.run_sync(
                coordinator.resolve_profile,
                profile_name or None,
                owner_canonical_user_id=owner_canonical_user_id,
            )
        )
        return selected_name, selected_profile, source.value

    resolved_objective_id = _resolve_objective_id_for_tool(
        agent,
        resolved_objective_id,
        task_id,
        "",
    )
    policy = agent.task_manager.operations_runtime.run_sync(
        agent.task_manager.objective_work_repository.get_policy,
        resolved_objective_id,
        owner_agent_id=agent.task_manager.operations_runtime.run_sync(
            agent.task_manager.durable_manager_scope_id
        ),
    )
    if policy is None:
        raise ValueError(
            f"Objective {resolved_objective_id!r} has no bounded execution policy."
        )
    coordinator._require_policy_user(policy, owner_canonical_user_id)
    selected_name = coordinator._normalize_profile_name(
        profile_name or policy.profile_name
    )
    selected_profile = ObjectiveWorkProfile(
        cadence_cron=policy.cadence_cron,
        cadence_timezone=policy.cadence_timezone,
        budget=policy.budget.model_copy(deep=True),
    )
    return selected_name, selected_profile, policy.profile_source.value


def _render_profile_layer(
    *,
    profile_name: str,
    profile: ObjectiveWorkProfile,
    make_default: bool,
) -> str:
    profile_payload = {
        "cadence_cron": profile.cadence_cron,
        "cadence_timezone": profile.cadence_timezone,
        "budget": profile.budget.model_dump(mode="json", exclude={"message_class"}),
    }
    layer_payload: Dict[str, Any] = {
        "schema_version": 2,
        "profiles": {profile_name: profile_payload},
    }
    if make_default:
        layer_payload["default_profile"] = profile_name
    return yaml.safe_dump(layer_payload, sort_keys=False, width=4096)


def _save_profile_layer(
    agent: "UniversalAgent",
    *,
    profile_name: str,
    profile_source: str,
    rendered_yaml: str,
    replace_existing: bool,
) -> tuple[str, str]:
    # Exported layers are team control files: they can be activated through
    # workspace.objective_execution_paths (relative to control/), so only this
    # trusted export writes them.
    layout = agent_layout(agent)
    control_dir = layout.control_dir
    output_dir = layout.objective_profiles_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    resolved_output_dir = output_dir.resolve()
    if not resolved_output_dir.is_relative_to(control_dir):
        raise ValueError(
            "The Objective execution profile export directory escapes the control folder."
        )
    relative_path = resolved_output_dir.relative_to(control_dir) / f"{profile_name}.yaml"
    output_path = resolved_output_dir / relative_path.name
    if output_path.exists() and not replace_existing:
        raise ValueError(
            f"Objective execution profile YAML already exists: {output_path}. "
            "Discuss replacement and set replace_existing only after agreement."
        )
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=resolved_output_dir,
        prefix=f".{profile_name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temp_path = Path(handle.name)
        handle.write(rendered_yaml)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temp_path, output_path)
    finally:
        temp_path.unlink(missing_ok=True)
    logging.getLogger(__name__).info(
        "Saved inert Objective execution profile policy layer. "
        "event=objective_execution_profile_yaml_saved profile_name=%s "
        "profile_source=%s path=%s",
        profile_name,
        profile_source,
        output_path,
    )
    return str(output_path), relative_path.as_posix()


def export_profile(
    profile_name: str = "",
    objective_id: str = "",
    make_default: bool = False,
    save: bool = False,
    confirmed: bool = False,
    replace_existing: bool = False,
) -> str:
    """Render or save one profile as an inert schema-v2 policy layer (``objective_work_profiles(action="export")``)."""

    agent = get_agent_ref()
    denial = _require_manager_tool(agent, "objective_work_profiles")
    if denial:
        return json.dumps(denial)
    current = _get_current_task_info(agent)
    if current is None or current.meta.requester_type != RequesterType.USER_REQUESTER:
        return json.dumps({"error": "direct_user_request_required"})
    owner, owner_denial = _private_user_scope(
        agent,
        resource_name="Objective work profiles",
    )
    if owner_denial is not None:
        return json.dumps(owner_denial)
    should_save = _coerce_tool_bool(save)
    if should_save and not _coerce_tool_bool(confirmed):
        return json.dumps(
            {
                "error": "Explicit user confirmation is required before saving the YAML layer."
            }
        )
    try:
        selected_name, selected_profile, profile_source = _resolve_profile_for_export(
            agent,
            profile_name=profile_name,
            objective_id=objective_id,
            owner_canonical_user_id=owner,
            task_id=current.task.task_id,
        )
        rendered_yaml = _render_profile_layer(
            profile_name=selected_name,
            profile=selected_profile,
            make_default=_coerce_tool_bool(make_default),
        )
        relative_path = Path("objective_execution_profiles") / f"{selected_name}.yaml"
        saved_path: str | None = None
        if should_save:
            saved_path, relative_value = _save_profile_layer(
                agent,
                profile_name=selected_name,
                profile_source=profile_source,
                rendered_yaml=rendered_yaml,
                replace_existing=_coerce_tool_bool(replace_existing),
            )
            relative_path = Path(relative_value)
        return json.dumps(
            {
                "profile_name": selected_name,
                "profile_source": profile_source,
                "schema_version": 2,
                "yaml": rendered_yaml,
                "saved": saved_path is not None,
                "saved_path": saved_path,
                "workspace_relative_path": relative_path.as_posix(),
                "activation_required": True,
                "activation_instruction": (
                    "Append the workspace-relative path to workspace.objective_execution_paths "
                    "after the earlier base layers, then restart the manager."
                ),
                "config_entry": relative_path.as_posix(),
            }
        )
    except (PermissionError, RuntimeError, TypeError, ValueError) as exc:
        return json.dumps({"error": str(exc)})
