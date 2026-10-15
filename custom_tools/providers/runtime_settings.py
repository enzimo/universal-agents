"""Runtime settings support for the trusted Strands tool surface."""

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, List, Optional

import yaml
from strands import tool

from custom_agents.model_capability_checks import (
    HubModelInfo,
    HuggingFaceAccountUnavailable,
    lookup_model_capability_candidates as build_model_capability_candidates,
)
from custom_agents.runtime_configuration_context import (
    build_runtime_configuration_context,
)
from custom_agents.runtime_settings_service import (
    RuntimeSettingChange,
    RuntimeSettingsService,
    runtime_settings_options_payload,
)
from custom_messages.enums import ControlType
from custom_tools.agent_catalog_port import lookup_agent_type
from custom_tools import file_access
from custom_tools.path_policy import PathPolicyError
from runtime_settings.instance_layout import InstanceLayout
from custom_tools.agent_team_port import current_agent_type_map
from custom_tools.provider_contract import ToolProvider
from custom_tools.providers._configuration import (
    _resolve_notify_user_instance_config_path,
)
if TYPE_CHECKING:
    from assistant_gateway.config import GatewayInstanceConfig
from custom_tools.runtime_context import (
    get_agent_ref,
)

logger = logging.getLogger(__name__)


def _huggingface_model_info_through_broker(repo_id: str) -> HubModelInfo:
    """Read a model's metadata with the linked Hugging Face account, through the broker.

    The broker's access gate decides (``cli:huggingface`` ``model.info``, read)
    and picks the account: the person's own link, else the team's. No token
    reaches this process.
    """

    from custom_tools.secure_cli_tool import _secure_cli_impl
    from secure_tools.hub_providers import HF_PROVIDER, MODEL_INFO

    result = _secure_cli_impl(
        HF_PROVIDER, MODEL_INFO, {"repo_id": repo_id}, "Look up model metadata for capability settings"
    )
    texts = [str(item.get("text") or "") for item in result.get("content") or []]
    try:
        payload = json.loads(texts[1]) if len(texts) > 1 else {}
    except json.JSONDecodeError:
        payload = {}
    if result.get("status") != "success":
        reason = str(payload.get("reason_code") or payload.get("error_code") or (texts[0] if texts else "")).strip()
        raise HuggingFaceAccountUnavailable(reason or "the broker did not answer")
    content = payload.get("content") if isinstance(payload.get("content"), dict) else {}
    return HubModelInfo(
        pipeline_tag=content.get("pipeline_tag"),
        tags=tuple(str(tag) for tag in content.get("tags") or ()),
    )


@tool
def get_self_status(info_type: ControlType = ControlType.STATUS_REQUEST_BASIC) -> str:
    """
    Get the current agent's status information. Can either request basic information (STATUS_REQUEST_BASIC) or expanded information (STATUS_REQUEST_FULL)

    Returns a JSON object containing the agent's ID, name, role, tools,
    execution state, queue/capacity metrics, paused state, and other diagnostic information.

    Returns:
        JSON string containing the agent's current AgentStatus.
    """
    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})

    status = agent.get_self_status(info_type=info_type)
    return status.model_dump_json()


@tool
def get_runtime_configuration_context(
    include_config_file_snapshot: bool = True, agent_types: Optional[List[str]] = None
) -> str:
    """
    Return non-secret runtime configuration context for this agent.

    Use this when you need to know where the workspace, skills directory, user docs,
    storage, broker URL, enabled apps, or instance config are configured, and before
    switching the model endpoint: ``model_endpoints`` lists the current endpoint and
    every endpoint whose key is already saved, with the base_url to stage. Secret
    values are omitted or reduced to configured booleans.

    To check other agent types' model settings (for example after changing
    them), pass their names in ``agent_types``: ``agent_type_models`` shows
    the saved values, which their agents use from their next turn. Status
    reports of idle agents show the old values until then.
    """
    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})
    try:
        known_agent_types: Optional[set[str]] = None
        if agent_types:
            try:
                known_agent_types = set(current_agent_type_map())
            except RuntimeError:
                logger.warning(
                    "Agent types are not configured; showing model settings without checking names. "
                    "event=agent_type_models_unchecked"
                )
        return json.dumps(
            build_runtime_configuration_context(
                agent,
                include_config_file_snapshot=include_config_file_snapshot,
                agent_types=list(agent_types or []),
                known_agent_types=known_agent_types,
            ),
            default=str,
        )
    except Exception as exc:
        return json.dumps({"error": str(exc)})


@tool
def lookup_model_capability_candidates(
    provider: str = "",
    model_id: str = "",
    openrouter_model_id: str = "",
    huggingface_repo_id: str = "",
    methods: str = "litellm_proxy,openrouter,litellm,huggingface",
    timeout_seconds: float = 10.0,
) -> str:
    """
    Look up facts about a model before switching to it: its context window,
    whether it reasons, and whether it accepts images, audio and files.

    Use it when the person asks to switch a model and before staging the
    switch. Sources, in order: the team's own LiteLLM server (the models it
    actually routes; checked only for the litellm provider), OpenRouter's
    model directory, LiteLLM's built-in model data, and Hugging Face. The
    result is evidence, not configuration: stage only what the person asked
    for plus the context window and input kinds the evidence supports (a
    switch resets them), and ask the person when the sources disagree or say
    nothing.

    Args:
        provider: Provider name. If omitted, uses the current agent provider.
        model_id: Model id as this agent would send it. If omitted, uses the current model.
        openrouter_model_id: Optional OpenRouter model id override.
        huggingface_repo_id: Optional Hugging Face repo id override.
        methods: Comma-separated subset of litellm_proxy, openrouter, litellm, huggingface.
        timeout_seconds: Per-request timeout for network lookups.
    """
    agent = get_agent_ref()
    resolved_provider = str(
        provider or getattr(agent, "model_provider", "") or ""
    ).strip()
    resolved_model_id = str(model_id or getattr(agent, "model_id", "") or "").strip()
    if not resolved_model_id:
        return json.dumps(
            {"error": "model_id is required when no current agent model is available."}
        )
    from custom_agents.strands_model_factory import normalize_model_provider

    # The team's LiteLLM server knows only the litellm provider's models. Its
    # key stays in trusted code: the lookup puts it in a request header only.
    uses_litellm = normalize_model_provider(resolved_provider or "litellm") == "litellm"
    api_base = str(getattr(agent, "litellm_api_base", "") or "") if uses_litellm else ""
    try:
        return json.dumps(
            build_model_capability_candidates(
                provider=resolved_provider,
                model_id=resolved_model_id,
                openrouter_model_id=openrouter_model_id,
                huggingface_repo_id=huggingface_repo_id,
                methods=methods,
                timeout_seconds=timeout_seconds,
                huggingface_account_lookup=_huggingface_model_info_through_broker,
                litellm_api_base=api_base,
                litellm_api_key=_endpoint_key(api_base),
            ),
            default=str,
        )
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def _endpoint_key(api_base: str) -> str | None:
    """The OpenAI-compatible endpoint's key for the model-info lookup, or None without one.

    Without a key the lookup still runs and reports what the server said.
    """

    if not api_base:
        return None
    from custom_agents.model_keys import ModelKeyError, process_model_keys
    from model_providers.endpoints import InvalidEndpointAddress, openai_compatible_endpoint

    try:
        return process_model_keys().resolve(openai_compatible_endpoint(api_base)).value or None
    except (ModelKeyError, InvalidEndpointAddress) as exc:
        logger.warning(
            "The model capability lookup runs without a key. event=model_capability_lookup_without_key error=%s", exc
        )
        return None


@tool
def validate_llm_overrides_yaml(content: str = "", path: str = "") -> str:
    """
    Validate persistent LLM override YAML without writing it.

    Pass content to validate a proposal. If content is omitted, this validates
    the team's current LLM override file (in the control folder), or a draft at
    `path` in this task's folders. Agent-authored live changes must use the
    staged runtime-settings workflow; validation never writes or applies changes.
    """
    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})
    try:
        from config_loader import validate_llm_override_payload

        config = _runtime_settings_config(agent)
        target_path = _control_llm_override_path(agent, config)
        if path:
            try:
                target_path = file_access.locate(path).path
            except PathPolicyError as exc:
                return json.dumps(
                    {
                        "valid": False,
                        "error": f"LLM override draft must be in this task's folders: {exc}",
                        "target_path": path,
                    }
                )

        source = "provided_content"
        yaml_text = str(content or "")
        if not yaml_text:
            source = "current_file"
            try:
                # A draft in the task's folders is read through the file library
                # (never through a link); the control file is trusted.
                yaml_text = (
                    file_access.read_text(target_path)[1] if path else target_path.read_text(encoding="utf-8")
                )
            except FileNotFoundError:
                return json.dumps(
                    {
                        "valid": True,
                        "errors": [],
                        "warnings": [
                            f"LLM override file does not exist yet: {target_path}"
                        ],
                        "target_path": str(target_path),
                        "source": source,
                        "summary": {"agent_types": [], "model_capability_entries": []},
                    }
                )
            except PathPolicyError as exc:
                return json.dumps({"valid": False, "error": str(exc), "target_path": str(target_path)})

        try:
            raw = yaml.safe_load(yaml_text) if yaml_text.strip() else {}
        except yaml.YAMLError as exc:
            return json.dumps(
                {
                    "valid": False,
                    "errors": [f"Invalid YAML: {exc}"],
                    "warnings": [],
                    "target_path": str(target_path),
                    "source": source,
                    "summary": {"agent_types": [], "model_capability_entries": []},
                },
                default=str,
            )
        result = validate_llm_override_payload(raw)
        return json.dumps(
            {
                **result,
                "target_path": str(target_path),
                "source": source,
                "write_guidance": (
                    "This validator is diagnostic only. Agent-authored provider, model, and reviewed capability "
                    "changes must use plan_runtime_setting_change or plan_runtime_settings_change followed by "
                    "apply_change. Complete-file maintenance and model-capability catalog edits "
                    "remain operator-owned until they have a typed settings path."
                ),
            },
            default=str,
        )
    except Exception as exc:
        return json.dumps({"error": str(exc)}, default=str)


@tool
def get_team_composition() -> str:
    """
    Inspect the team's TEAM_AGENTS.yaml composition file (in the control folder).

    Returns the persisted startup-agent overrides, generated-bundle location,
    ephemeral TTL policy, and the agent types the team can start now. Startup overrides
    are read by the headless supervisor at launch; they are not hot-loaded.
    """
    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})
    try:
        try:
            config = _runtime_settings_config(agent)
        except Exception:
            config = None
        composition_path, composition = _load_team_composition_for_agent(agent, config)
        control_dir = _agent_control_dir(agent, config)
        from custom_agents.team_composition import generated_agent_bundle_root

        agent_type_map = current_agent_type_map()

        return json.dumps(
            {
                "team_composition_path": str(composition_path),
                "exists": composition_path.exists(),
                "control_dir": str(control_dir),
                "generated_agent_bundles_dir": str(
                    generated_agent_bundle_root(
                        control_dir=control_dir, config=composition
                    )
                ),
                "composition": composition.model_dump(mode="json", exclude_none=True),
                "registered_agent_types": sorted(agent_type_map),
                "application_model": {
                    "startup_agent_overrides": "read at headless supervisor launch",
                    "generated_agent_definitions": "available as soon as they are created; no restart",
                    "ephemeral_ttl_policy": "read when instantiate_agent creates a dynamic child",
                    "hot_loading": False,
                    "direct_restart_available": (
                        "TeamArchitect can stage supervised manager/gateway/all child-process restarts, but "
                        "dispatch requires an exact authenticated user decision for runtime.restart after an "
                        "impact snapshot and state recheck. It cannot reboot the host/container or rebuild the "
                        "already running supervisor startup command list."
                    ),
                },
            },
            default=str,
        )
    except Exception as exc:
        return json.dumps({"error": str(exc)}, default=str)


@tool
def set_team_startup_agent(
    agent_type: str,
    count: int = 1,
    enabled: bool = True,
    mode: str = "merge",
) -> str:
    """
    Stage a TEAM_AGENTS.yaml startup-agent override; apply it with apply_change.

    Startup overrides are read by the headless supervisor at launch. The
    change does not hot-load already running supervisor children.
    """
    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})
    try:
        config = _runtime_settings_config(agent)
        control_dir = _agent_control_dir(agent, config)
        composition_path, composition = _load_team_composition_for_agent(agent, config)
        if enabled:
            _validate_agent_type_available(
                control_dir=control_dir,
                composition=composition,
                agent_type=agent_type,
            )
        settings_service = RuntimeSettingsService(agent)
        plan = settings_service.stage(
            [
                {
                    "path": "team.startup_agents.count",
                    "value": count,
                    "target_scope": "agent_type",
                    "target_agent_type": agent_type,
                },
                {
                    "path": "team.startup_agents.enabled",
                    "value": enabled,
                    "target_scope": "agent_type",
                    "target_agent_type": agent_type,
                },
                {
                    "path": "team.startup_agents.mode",
                    "value": mode,
                    "target_scope": "team",
                },
            ],
            reason=f"Update startup roster for {agent_type}",
        )
        return json.dumps(
            {
                **plan,
                "team_composition_path": str(composition_path),
                "application_model": {
                    "startup_override": "applies on the next headless supervisor launch",
                    "hot_loading": False,
                    "user_notice": (
                        "Tell the user they should reboot or relaunch the Universal Agents system for this startup "
                        "agent roster change to take effect."
                    ),
                    "direct_restart_available": (
                        "TeamArchitect can request supervised runtime restarts, but cannot directly reboot the "
                        "host/container or rebuild the already running supervisor startup command list."
                    ),
                },
            },
            default=str,
        )
    except Exception as exc:
        return json.dumps({"error": str(exc)}, default=str)


@tool
def set_ephemeral_agent_policy(
    agent_type: str,
    idle_ttl_seconds: Optional[float] = None,
    enabled: bool = True,
) -> str:
    """
    Stage the idle TTL policy for dynamically spawned ephemeral agents; apply
    it with apply_change.

    The policy is read when `instantiate_agent(...)` creates a dynamic child.
    Startup-supervised agents ignore this setting.
    """
    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})
    try:
        config = _runtime_settings_config(agent)
        composition_path, _composition = _load_team_composition_for_agent(agent, config)
        settings_service = RuntimeSettingsService(agent)
        plan = settings_service.stage(
            [
                {
                    "path": "team.ephemeral_agents.enabled",
                    "value": enabled,
                    "target_scope": "agent_type",
                    "target_agent_type": agent_type,
                },
                {
                    "path": "team.ephemeral_agents.idle_ttl_seconds",
                    "value": idle_ttl_seconds,
                    "target_scope": "agent_type",
                    "target_agent_type": agent_type,
                },
            ],
            reason=f"Update ephemeral-agent policy for {agent_type}",
        )
        return json.dumps(
            {
                **plan,
                "team_composition_path": str(composition_path),
                "application_model": {
                    "ephemeral_ttl_policy": "read by future instantiate_agent calls",
                    "hot_loading": True,
                },
            },
            default=str,
        )
    except Exception as exc:
        return json.dumps({"error": str(exc)}, default=str)


def _runtime_settings_config(agent: Any):
    config_path = _resolve_notify_user_instance_config_path(agent)
    if not config_path:
        raise ValueError(
            "No instance_config.yaml path is available for runtime settings changes."
        )
    from assistant_gateway.config import GatewayInstanceConfig

    return GatewayInstanceConfig.load_for_process(config_path)


def _agent_control_dir(agent: Any, config: "GatewayInstanceConfig | None" = None) -> Path:
    """The team's control folder, from the agent's or the instance's root."""

    root = str(getattr(agent, "instance_root", None) or "").strip()
    if root:
        return InstanceLayout.at(root).control_dir
    if config is not None:
        return config.layout.control_dir
    raise ValueError("No instance root is configured, so team control files cannot be found.")


def _control_llm_override_path(agent: Any, config: Any) -> Path:
    from config_loader import resolve_llm_override_path

    control_dir = _agent_control_dir(agent, config)
    resolved_path = resolve_llm_override_path(
        instance_root=str(control_dir.parent),
        explicit_override_path=getattr(agent, "llm_override_path", None),
    )
    if control_dir not in resolved_path.parents:
        raise ValueError(
            "Current-agent model changes must persist to the team's LLM override file; "
            f"resolved path {resolved_path} is outside the control folder {control_dir}."
        )
    return resolved_path


def _control_team_composition_path(agent: Any, config: Any | None = None) -> Path:
    from custom_agents.team_composition import resolve_team_composition_path

    control_dir = _agent_control_dir(agent, config)
    composition_path = resolve_team_composition_path(
        control_dir=control_dir,
        explicit_path=getattr(agent, "team_composition_path", None),
    )
    if control_dir not in composition_path.parents:
        raise ValueError(
            "Team composition changes must persist to the team's TEAM_AGENTS.yaml file; "
            f"resolved path {composition_path} is outside the control folder {control_dir}."
        )
    return composition_path


def _load_team_composition_for_agent(agent: Any, config: Any | None = None):
    from custom_agents.team_composition import load_team_composition

    path = _control_team_composition_path(agent, config)
    return path, load_team_composition(path)


def _validate_agent_type_available(
    *,
    control_dir: Path,
    composition: Any,
    agent_type: str,
) -> dict[str, Any]:
    from custom_agents.team_composition import generated_agent_bundle_root

    generated_root = generated_agent_bundle_root(
        control_dir=control_dir, config=composition
    )
    additional_paths = [generated_root] if generated_root.exists() else []
    definition = lookup_agent_type(
        additional_paths,
        agent_type=agent_type,
        include_configured=True,
    )
    if definition is None:
        raise ValueError(
            f"Agent type {agent_type!r} is not available in the hydrated catalog."
        )
    return {
        "agent_type": agent_type,
        "display_name": definition.display_name,
        "role": definition.role,
        "description": definition.description,
        "source_paths": list(definition.source_paths),
    }


def _resolve_ephemeral_idle_ttl(agent: Any, agent_type: str) -> float | None:
    try:
        from custom_agents.team_composition import ephemeral_idle_ttl_for_agent_type

        _, composition = _load_team_composition_for_agent(agent)
        return ephemeral_idle_ttl_for_agent_type(composition, agent_type)
    except Exception as exc:
        logger.warning(
            "Failed to resolve ephemeral agent TTL; dynamic spawn will be persistent. "
            "event=ephemeral_ttl_resolve_failed agent_type=%s error=%s",
            agent_type,
            exc,
        )
        return None


@tool
def list_runtime_settings_options() -> str:
    """
    List typed TeamArchitect self/team settings and their decision classes.

    Stage a change with a plan tool and apply it with apply_change: with
    authorization on a person approves the exact plan (a `user_authorized`
    setting may also be approved by the person it belongs to); with it off,
    the person's conversational agreement applies it. `operator_only`
    settings cannot be staged. For models, `llm.*` is the instance baseline,
    `workspace_default.llm.*` is `LLM_OVERRIDES.yaml`'s workspace-wide
    `default:` entry, and `current_agent.llm.*` is one flat
    `agent_types.<AgentType>:` entry.
    """
    return json.dumps(runtime_settings_options_payload(), default=str)


def _stage_runtime_settings_change(
    agent: Any,
    changes: List[RuntimeSettingChange],
    *,
    reason: str = "",
    target_scope: str = "",
    target_agent_type: str = "",
) -> str:
    try:
        plan = RuntimeSettingsService(agent).stage(
            changes,
            reason=reason,
            target_scope=target_scope,
            target_agent_type=target_agent_type,
        )
        return json.dumps(plan, default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)}, default=str)


@tool
def plan_runtime_settings_change(
    changes: List[RuntimeSettingChange],
    reason: str = "",
    target_scope: str = "",
    target_agent_type: str = "",
) -> str:
    """
    Validate and immutably stage one atomic-source settings change plan.

    Each change must contain `path` and `value` and may override
    `target_scope`/`target_agent_type` for that item. Top-level target fields
    remain defaults for items that omit them. Scope is self, agent_type, or
    team. The plan records old/new values, source hashes, actor, expiry,
    decision class, and a canonical digest.

    Do not use target_agent_type="default" for a workspace-wide model change;
    use the team-scoped `workspace_default.llm.*` paths.
    """
    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})
    return _stage_runtime_settings_change(
        agent,
        changes,
        reason=reason,
        target_scope=target_scope,
        target_agent_type=target_agent_type,
    )


@tool
def plan_runtime_setting_change(
    path: str,
    value: Any,
    reason: str = "",
    target_scope: str = "",
    target_agent_type: str = "",
) -> str:
    """
    Validate and stage one runtime settings change without applying it.

    Prefer this for single-setting updates such as
    current_agent.llm.supports_image_input=true. Use
    plan_runtime_settings_change only when staging multiple settings at once.
    """
    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})
    return _stage_runtime_settings_change(
        agent,
        [{"path": path, "value": value}],
        reason=reason,
        target_scope=target_scope,
        target_agent_type=target_agent_type,
    )


# Reading the agent's own status and configuration (catalog fn:runtime_status,
# agent_runtime); staging changes is the team lead's (fn:runtime_settings).
# Every agent reads its own status; the roster and settings helpers serve the
# team lead's settings changes (fn:runtime_settings in the tool catalog).
RUNTIME_STATUS_PROVIDER = ToolProvider(
    name="runtime_status",
    tools=(
        get_self_status,
        get_runtime_configuration_context,
    ),
)
MODEL_LOOKUP_PROVIDER = ToolProvider(name="model_lookup", tools=(lookup_model_capability_candidates,))
PROVIDER = ToolProvider(
    name="runtime_settings",
    tools=(
        get_team_composition,
        list_runtime_settings_options,
        validate_llm_overrides_yaml,
        set_team_startup_agent,
        set_ephemeral_agent_policy,
        plan_runtime_settings_change,
        plan_runtime_setting_change,
    ),
)
