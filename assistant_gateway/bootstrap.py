"""Bootstrap helpers for Phase 1 gateway instances."""

from __future__ import annotations

import os
from pathlib import Path
import logging
import re
import json
from typing import Mapping, Sequence

from access_control.people import PERSON_ROLE_OWNER
from assistant_gateway.config import GatewayInstanceConfig
from assistant_gateway.ports.repositories import IdentityRepository
from assistant_gateway.store import GatewayStateStore
from custom_agents.agent_catalog import load_agent_catalog, resolve_agent_definition_paths
from custom_messages.contracts.runtime_v1 import (
    DELIVERY_CHANNEL_DISCORD,
    DELIVERY_CHANNEL_PHOTON,
    DELIVERY_CHANNEL_SIGNAL,
    DELIVERY_CHANNEL_TELEGRAM,
    DELIVERY_CHANNEL_WHATSAPP,
    TEAM_ARCHITECT_AGENT_TYPE,
)
from custom_agents.team_composition import TEAM_COMPOSITION_SCHEMA_VERSION
from custom_messages.contracts.access_v2 import AccountSharing, OnboardingModelKeyV1
from internal_credentials import parse_env_file
from runtime_settings.services import REMOVED_LINKED_KEY_ENV_VARS
from assistant_gateway.onboarding_account_keys import (
    ONBOARDING_KEY_FIELDS,
    keys_from_env_values,
    write_handover,
)


BOOTSTRAP_FILES = {
    "SOUL.md": """# Assistant Identity

Name: {assistant_name}
Purpose: {assistant_purpose}

## Response Style
{assistant_response_style}

## Autonomy
{assistant_autonomy}
""",
    "LLM_OVERRIDES.yaml": """# Persistent model overrides for this assistant personality.
# These values win over config.yaml for agents of this instance.
#
# Leave provider/model_id blank to fall back to config.yaml.
# `supports_*: null` means unknown. Populate these only from configured facts
# or user-approved provider/model research.
# A model-scoped `reasoning_summary: null` disables a broader summary setting
# after that exact OpenAI reasoning model rejects reasoning.summary.
default:
  provider: "{default_provider}"
  model_id: "{default_model_id}"
  reasoning_effort: {default_reasoning_effort}
  reasoning_summary: {default_reasoning_summary}
  litellm_api_mode: {default_litellm_api_mode}
  context_window_tokens: {default_context_window_tokens}
  supports_image_input: {default_supports_image_input}
  supports_audio_input: {default_supports_audio_input}
  supports_file_input: {default_supports_file_input}

model_capabilities:
  # provider_name:
  #   model_id:
  #     reasoning_summary: null
  #     supports_image_input: null
  #     supports_audio_input: null
  #     supports_file_input: null

agent_types:
{agent_type_llm_overrides}
""",
    "TEAM_TEMPLATES.yaml": """# Reusable starting teams for this assistant.
# The manager may inspect and instantiate these operator/onboarding-authored
# templates. Agent generic file and shell tools cannot mutate this catalog.
version: 1

templates:
{team_templates}
""",
    "TEAM_AGENTS.yaml": """# Team lead-managed agent composition.
# Restart-applied startup overrides are read by the headless supervisor at launch.
# Generated agent bundles live under `generated_agent_bundles_dir` (relative to
# this control folder) by default.
schema_version: {team_composition_schema_version}

startup_agents:
  mode: merge
  agents: []

ephemeral_agents:
  default_idle_ttl_seconds: 600
  agent_types: {{}}

generated_agent_bundles_dir: agent_bundles/team_lead_generated
notes: ""
""",
}

DEFAULT_ASSISTANT_PURPOSE = "Help me solve problems pragmatically."
DEFAULT_ASSISTANT_RESPONSE_STYLE = "Concise and direct. Talk to me normally and do not use jargon unless something requires technical precision."
DEFAULT_ASSISTANT_AUTONOMY = "Ask clarifying questions when ambiguity or risk is high."

logger = logging.getLogger(__name__)


def _yaml_string(value: object) -> str:
    return json.dumps("" if value is None else str(value))


def _yaml_scalar(value: object) -> str:
    return json.dumps(value)


def _render_agent_type_llm_overrides(
    *,
    config: GatewayInstanceConfig,
    manager_provider: str,
    manager_model_id: str,
) -> str:
    lines: list[str] = []
    catalog = load_agent_catalog(
        resolve_agent_definition_paths(
            config.agent.agent_definition_paths,
            instance_root=str(config.instance_root_path),
        ),
        include_configured=False,
    )
    for agent_type in catalog.agent_types:
        provider = manager_provider if agent_type == TEAM_ARCHITECT_AGENT_TYPE else ""
        model_id = manager_model_id if agent_type == TEAM_ARCHITECT_AGENT_TYPE else ""
        manager_entry = agent_type == TEAM_ARCHITECT_AGENT_TYPE
        lines.extend(
            [
                f"  {agent_type}:",
                f"    provider: {_yaml_string(provider)}",
                f"    model_id: {_yaml_string(model_id)}",
            ]
        )
        if manager_entry:
            lines.extend(
                [
                    f"    reasoning_effort: {_yaml_scalar(config.llm.reasoning_effort)}",
                    f"    reasoning_summary: {_yaml_scalar(config.llm.reasoning_summary)}",
                    f"    litellm_api_mode: {_yaml_scalar(config.llm.litellm_api_mode)}",
                    f"    context_window_tokens: {_yaml_scalar(config.llm.context_window_tokens)}",
                    f"    supports_image_input: {_yaml_scalar(config.llm.supports_image_input)}",
                    f"    supports_audio_input: {_yaml_scalar(config.llm.supports_audio_input)}",
                    f"    supports_file_input: {_yaml_scalar(config.llm.supports_file_input)}",
                ]
            )
    return "\n".join(lines)


def _render_team_templates(config: GatewayInstanceConfig) -> str:
    catalog = load_agent_catalog(
        resolve_agent_definition_paths(
            config.agent.agent_definition_paths,
            instance_root=str(config.instance_root_path),
        ),
        include_configured=False,
    )
    templates = catalog.team_templates
    if not templates:
        return "  {}"

    lines: list[str] = []
    for template_name, template in templates.items():
        lines.append(f"  {template_name}:")
        lines.append(f"    description: {_yaml_string(template.description)}")
        lines.append("    members:")
        for member in template.members:
            lines.append(f"      - agent_type: {_yaml_string(member.agent_type)}")
            lines.append(f"        count: {int(member.count)}")
            if member.mcp_profile_names:
                lines.append("        mcp_profile_names:")
                for profile_name in member.mcp_profile_names:
                    lines.append(f"          - {_yaml_string(profile_name)}")
            if member.notes:
                lines.append(f"        notes: {_yaml_string(member.notes)}")
    return "\n".join(lines)


def _format_env_value(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_./:@%+=,-]+", value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _is_credential_store_key(key: str) -> bool:
    """A key that is a linked account in the credential store now, never an environment variable."""

    return key in REMOVED_LINKED_KEY_ENV_VARS or key.removesuffix("_FILE") in REMOVED_LINKED_KEY_ENV_VARS


def _env_line_key(line: str) -> str | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in line:
        return None
    return line.split("=", 1)[0].strip()


def _merge_source_env_file(source: Path, destination: Path) -> dict[str, str]:
    """Merge a source dotenv file into destination, treating source keys as explicit overrides.

    Keys that are linked accounts in the credential store now
    (``REMOVED_LINKED_KEY_ENV_VARS``) are not copied, because startup refuses
    them in team.env; they are returned so the caller can hand the Exa and
    Brave Search keys to the credential store.
    """

    all_source_values = parse_env_file(source)
    held_back = {key: value for key, value in all_source_values.items() if _is_credential_store_key(key)}
    source_values = {key: value for key, value in all_source_values.items() if key not in held_back}
    if destination.exists():
        lines = _merged_env_lines(destination.read_text(encoding="utf-8").splitlines(), source_values)
    else:
        source_lines = source.read_text(encoding="utf-8").splitlines()
        lines = [line for line in source_lines if _env_line_key(line) not in held_back]
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return held_back


def _merged_env_lines(existing_lines: list[str], source_values: Mapping[str, str]) -> list[str]:
    """The existing lines with source values replacing matching keys, then the new keys."""

    seen: set[str] = set()
    output: list[str] = []
    for raw_line in existing_lines:
        key = _env_line_key(raw_line)
        if key is not None:
            seen.add(key)
        output.append(f"{key}={_format_env_value(source_values[key])}" if key in source_values else raw_line)
    missing_keys = sorted(key for key in source_values if key not in seen)
    if missing_keys:
        if output and output[-1].strip():
            output.append("")
        output.append("# Merged from onboarding --base-env-file. Source values override matching keys.")
        output += [f"{key}={_format_env_value(source_values[key])}" for key in missing_keys]
    return output


def hand_over_onboarding_keys(
    config: GatewayInstanceConfig,
    keys: Mapping[str, str],
    *,
    model_keys: Sequence[OnboardingModelKeyV1] = (),
    sharing: Mapping[str, AccountSharing] | None = None,
) -> tuple[Path, ...]:
    """Leave onboarding keys and sharing choices for the gateway (``onboarding_account_keys``); the file, if written."""

    if not keys and not model_keys and not sharing:
        return ()
    person_id = str(getattr(config.user, "canonical_user_id", "") or "").strip()
    if not person_id:
        logger.error(
            "Onboarding keys were not kept: the instance has no first user to link them for. "
            "event=onboarding_keys_no_person instance_name=%s tool_ids=%s",
            config.instance_name,
            ",".join(sorted(keys)),
        )
        return ()
    path = write_handover(
        config.instance_root,
        team_id=config.manager.team_id,
        person_id=person_id,
        keys=keys,
        model_keys=model_keys,
        sharing=sharing,
    )
    return (path,) if path is not None else ()


def _hand_over_env_file_keys(config: GatewayInstanceConfig, held_back: Mapping[str, str]) -> tuple[Path, ...]:
    """Hand over the Exa and Brave keys an imported env file held; log the keys that cannot be."""

    keys = keys_from_env_values(held_back)
    handed = {field.former_env_name for field in ONBOARDING_KEY_FIELDS if field.tool_id in keys}
    for name in sorted(set(held_back) - handed):
        logger.warning(
            "An imported env file set a key that is kept in the credential store now; it was not copied. "
            "Link it in chat instead (%s). event=onboarding_env_key_not_copied name=%s",
            REMOVED_LINKED_KEY_ENV_VARS.get(name.removesuffix("_FILE"), "/link"),
            name,
        )
    return hand_over_onboarding_keys(config, keys)


def _resolve_instance_root_hint_path() -> Path | None:
    raw_hint_path = str(os.getenv("TEAM_INSTANCE_ROOT_HINT_FILE", "")).strip()
    if raw_hint_path:
        return Path(raw_hint_path).expanduser().resolve()

    raw_data_root = str(os.getenv("TEAM_DATA_ROOT", "")).strip()
    if not raw_data_root:
        return None
    return (Path(raw_data_root).expanduser().resolve() / ".team-instance-root").resolve()


def _write_instance_root_hint(config: GatewayInstanceConfig) -> Path | None:
    hint_path = _resolve_instance_root_hint_path()
    if hint_path is None:
        return None
    hint_path.parent.mkdir(parents=True, exist_ok=True)
    hint_path.write_text(f"{config.instance_root_path}\n", encoding="utf-8")
    return hint_path


def _chat_scope_for_seeded_channel(config: GatewayInstanceConfig, channel_name: str, chat_id: str | None) -> str:
    if channel_name == "cli":
        return str(chat_id or f"cli:{config.instance_name}").strip()
    return ""


def _first_legacy_channel_identity(values: object) -> str | None:
    if not isinstance(values, list):
        return None
    for value in values:
        normalized = str(value or "").strip()
        if normalized:
            return normalized
    return None


def _legacy_admin_channel_binding_candidates(config: GatewayInstanceConfig) -> list[tuple[str, str]]:
    apps = config.interactive_apps
    candidates: list[tuple[str, str]] = []
    for channel_name, external_user_id in (
        (DELIVERY_CHANNEL_TELEGRAM, _first_legacy_channel_identity(apps.telegram.allowed_user_ids)),
        (DELIVERY_CHANNEL_DISCORD, _first_legacy_channel_identity(apps.discord.allowed_user_ids)),
        (DELIVERY_CHANNEL_WHATSAPP, _first_legacy_channel_identity(apps.whatsapp.allowed_sender_ids)),
        (DELIVERY_CHANNEL_SIGNAL, _first_legacy_channel_identity(apps.signal.allowed_sender_ids)),
        (DELIVERY_CHANNEL_PHOTON, _first_legacy_channel_identity(apps.photon.allowed_sender_ids)),
    ):
        if external_user_id:
            candidates.append((channel_name, external_user_id))
    return candidates


def seed_default_gateway_user(
    config: GatewayInstanceConfig,
    *,
    env: Mapping[str, str] | None = None,
    state_store: GatewayStateStore | None = None,
) -> bool:
    """Create the onboarded canonical user, membership, and CLI binding."""

    users_config = getattr(getattr(config, "gateway", None), "users", None)
    if not getattr(users_config, "enabled", False):
        return False
    canonical_user_id = str(getattr(config.user, "canonical_user_id", "") or "").strip()
    if not canonical_user_id:
        return False

    if state_store is None:
        storage_settings = config.resolve_state_storage_settings(env=env)
        try:
            state_store = GatewayStateStore(
                storage_settings.sqlite_path or "",
                storage_settings=storage_settings,
            )
        except Exception as exc:
            logger.warning(
                "Default gateway user seed skipped because state storage is unavailable. "
                "event=onboarding_default_user_seed_unavailable instance_name=%s backend=%s error_type=%s",
                config.instance_name,
                getattr(storage_settings, "backend", ""),
                type(exc).__name__,
            )
            return False
    with state_store.identity_unit_of_work() as unit_of_work:
        _seed_default_gateway_identity(
            config,
            identity=unit_of_work.identity,
            canonical_user_id=canonical_user_id,
        )
        unit_of_work.commit()
    return True


def _seed_default_gateway_identity(
    config: GatewayInstanceConfig,
    *,
    identity: IdentityRepository,
    canonical_user_id: str,
) -> None:
    """Seed all bootstrap identity rows in one caller-owned transaction."""

    existing = identity.get_gateway_user(canonical_user_id)
    if existing is None:
        identity.create_gateway_user(
            canonical_user_id=canonical_user_id,
            display_name=config.user.display_name,
            timezone=config.user.timezone,
            metadata={"seeded_from": "onboarding"},
        )
    else:
        identity.create_gateway_user(
            canonical_user_id=canonical_user_id,
            display_name=existing.display_name or config.user.display_name,
            timezone=existing.timezone or config.user.timezone,
            metadata=existing.metadata,
            status=existing.status,
        )

    # The instance's user becomes the team's first owner. An existing
    # membership is left as it is: a person may have changed its role with
    # /user role since, and instances set up before owners existed keep their
    # administrator until someone makes them an owner.
    team_id = config.gateway.team_id
    if not any(
        membership.team_id == team_id
        for membership in identity.list_team_memberships(canonical_user_id)
    ):
        identity.create_team_membership(
            canonical_user_id=canonical_user_id,
            team_id=team_id,
            role=PERSON_ROLE_OWNER,
            status="active",
        )
        logger.info(
            "Seeded the instance's user as the team's owner. "
            "event=default_user_owner_seeded canonical_user_id=%s team_id=%s",
            canonical_user_id,
            team_id,
        )

    channel_name = "cli"
    user_id = canonical_user_id
    chat_id = f"cli:{config.instance_name}"
    chat_scope = _chat_scope_for_seeded_channel(config, channel_name, chat_id)
    if identity.get_active_channel_binding(
        channel_name=channel_name,
        external_user_id=user_id,
        chat_scope=chat_scope,
    ) is None:
        identity.create_channel_binding(
            canonical_user_id=canonical_user_id,
            channel_name=channel_name,
            external_user_id=user_id,
            chat_scope=chat_scope,
            metadata={"seeded_from": "onboarding"},
        )

    for legacy_channel_name, legacy_external_id in _legacy_admin_channel_binding_candidates(config):
        legacy_scope = _chat_scope_for_seeded_channel(config, legacy_channel_name, None)
        existing_binding = identity.get_active_channel_binding(
            channel_name=legacy_channel_name,
            external_user_id=legacy_external_id,
            chat_scope=legacy_scope,
        )
        if existing_binding is None:
            binding = identity.create_channel_binding(
                canonical_user_id=canonical_user_id,
                channel_name=legacy_channel_name,
                external_user_id=legacy_external_id,
                chat_scope=legacy_scope,
                metadata={"seeded_from": "legacy_allowlist_first_entry"},
            )
            identity.record_gateway_user_event(
                event_type="legacy_admin_channel_binding_seed",
                canonical_user_id=canonical_user_id,
                payload={
                    "team_id": config.gateway.team_id,
                    "channel_name": legacy_channel_name,
                    "external_user_id": legacy_external_id,
                    "chat_scope": legacy_scope,
                    "binding_id": binding.binding_id,
                },
            )
        elif existing_binding.canonical_user_id != canonical_user_id:
            logger.warning(
                "Legacy admin channel binding seed skipped because identity is already bound. "
                "event=legacy_admin_channel_binding_conflict channel_name=%s external_user_id=%s "
                "existing_canonical_user_id=%s admin_canonical_user_id=%s",
                legacy_channel_name,
                legacy_external_id,
                existing_binding.canonical_user_id,
                canonical_user_id,
            )

    default_target = config.delivery.default_target
    if default_target.channel_name and default_target.chat_id:
        identity.set_gateway_user_default_channel(
            canonical_user_id=canonical_user_id,
            channel_name=default_target.channel_name,
            chat_id=default_target.chat_id,
            thread_id=default_target.thread_id or None,
        )
    identity.record_gateway_user_event(
        event_type="onboarding_default_user_seed",
        canonical_user_id=canonical_user_id,
        payload={
            "team_id": config.gateway.team_id,
            "channel_name": channel_name,
            "chat_scope": chat_scope,
        },
    )


def bootstrap_instance(
    config: GatewayInstanceConfig,
    *,
    source_env_file_path: str | None = None,
    assistant_purpose: str | None = None,
    assistant_response_style: str | None = None,
    assistant_autonomy: str | None = None,
    expected_config_revision: str | None = None,
) -> list[Path]:
    created_paths: list[Path] = []
    root = config.instance_root_path
    root.mkdir(parents=True, exist_ok=True)
    layout = config.layout
    layout.ensure_directories()

    # Control files are written only by trusted runtime services from here on;
    # agent tools and agent-run code cannot open control/.
    for filename in BOOTSTRAP_FILES:
        destination = layout.control_dir / filename
        if destination.exists():
            continue
        destination.write_text(
            render_bootstrap_control_file(
                config,
                filename,
                assistant_purpose=assistant_purpose,
                assistant_response_style=assistant_response_style,
                assistant_autonomy=assistant_autonomy,
            ),
            encoding="utf-8",
        )
        created_paths.append(destination)

    if source_env_file_path and config.launch.env_file_path:
        source_env_path = Path(source_env_file_path).expanduser().resolve()
        destination = Path(config.launch.env_file_path).expanduser().resolve()
        held_back = _merge_source_env_file(source_env_path, destination)
        created_paths.append(destination)
        created_paths.extend(_hand_over_env_file_keys(config, held_back))

    config_path = config.save(expected_revision=expected_config_revision)
    if config_path not in created_paths:
        created_paths.append(config_path)
    hint_path = _write_instance_root_hint(config)
    if hint_path is not None and hint_path not in created_paths:
        created_paths.append(hint_path)
    return created_paths


def render_bootstrap_control_file(
    config: GatewayInstanceConfig,
    filename: str,
    *,
    assistant_purpose: str | None = None,
    assistant_response_style: str | None = None,
    assistant_autonomy: str | None = None,
) -> str:
    """Render defaults without writing, also used to identify transfer choices."""
    return BOOTSTRAP_FILES[filename].format(
        assistant_name=config.assistant.display_name,
        assistant_purpose=assistant_purpose or DEFAULT_ASSISTANT_PURPOSE,
        assistant_response_style=assistant_response_style
        or DEFAULT_ASSISTANT_RESPONSE_STYLE,
        assistant_autonomy=assistant_autonomy or DEFAULT_ASSISTANT_AUTONOMY,
        default_provider=config.llm.provider or "",
        default_model_id=config.llm.model_id or "",
        default_reasoning_effort=_yaml_scalar(config.llm.reasoning_effort),
        default_reasoning_summary=_yaml_scalar(config.llm.reasoning_summary),
        default_litellm_api_mode=_yaml_scalar(config.llm.litellm_api_mode),
        default_context_window_tokens=_yaml_scalar(config.llm.context_window_tokens),
        default_supports_image_input=_yaml_scalar(config.llm.supports_image_input),
        default_supports_audio_input=_yaml_scalar(config.llm.supports_audio_input),
        default_supports_file_input=_yaml_scalar(config.llm.supports_file_input),
        manager_provider=config.llm.provider or "",
        manager_model_id=config.llm.model_id or "",
        agent_type_llm_overrides=_render_agent_type_llm_overrides(
            config=config,
            manager_provider=config.llm.provider or "",
            manager_model_id=config.llm.model_id or "",
        ),
        team_templates=_render_team_templates(config),
        team_composition_schema_version=TEAM_COMPOSITION_SCHEMA_VERSION,
    )
