"""Transactional persistence handler for workspace LLM profile settings.

Authorization and plan lifecycle stay in ``RuntimeSettingsService``.  This
module owns the lower-level transaction after an authorized plan has selected
the workspace LLM override target: candidate construction, provider preflight,
optimistic persistence, reread verification, live activation, and rollback.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Protocol, Sequence

import yaml

from custom_agents.runtime_setting_registry import (
    CURRENT_AGENT_LLM_PATH_PREFIX,
    INSTANCE_LLM_PATH_PREFIX,
    WORKSPACE_DEFAULT_LLM_PATH_PREFIX,
)
from config_backup_retention import prune_config_backups
from custom_agents.governance.authorized_operations import (
    atomic_write_bytes,
    sha256_bytes,
)
from custom_messages.contracts.runtime_v1 import TEAM_ARCHITECT_AGENT_TYPE
from custom_agents.model_keys import process_model_keys
from custom_agents.model_runtime_profile import (
    MODEL_CAPABILITY_FIELDS,
    MODEL_PROFILE_FIELDS,
    MODEL_TUNING_FIELDS,
    RuntimeLLMProfile,
    runtime_llm_profile_from_agent,
    stale_model_fields,
)
from secure_tool_broker.redaction import redact_text


LOGGER = logging.getLogger(__name__)
WORKSPACE_LLM_PERSISTENCE_TARGET = "workspace_llm_overrides"

_PREFLIGHT_PATHS = frozenset(
    f"{prefix}{field}"
    for prefix in (CURRENT_AGENT_LLM_PATH_PREFIX, WORKSPACE_DEFAULT_LLM_PATH_PREFIX, INSTANCE_LLM_PATH_PREFIX)
    for field in ("provider", "model_id", "reasoning_effort", "reasoning_summary", "litellm_api_mode")
)


class LLMOverrideChange(Protocol):
    path: str
    value: Any
    target_agent_type: str | None


class LLMOverrideSourceSnapshot(Protocol):
    existed: bool
    sha256: str


@dataclass(frozen=True)
class RuntimeLLMModelCandidate:
    target: str
    profile: RuntimeLLMProfile
    paths: frozenset[str]
    request_settings: Mapping[str, Any] | None = None


@dataclass
class _PreparedOverrides:
    payload: dict[str, Any]
    new_bytes: bytes
    expected_entries: dict[str, dict[str, Any]]
    default_changes: dict[str, Any]
    default_paths: set[str]
    default_profile: RuntimeLLMProfile
    current_profile: RuntimeLLMProfile | None
    current_paths: set[str]
    candidates: list[RuntimeLLMModelCandidate]


def _read_yaml_mapping(path: Path, *, must_exist: bool = False) -> dict[str, Any]:
    if not path.exists():
        if must_exist:
            raise FileNotFoundError(f"Required settings source does not exist: {path}")
        return {}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML in settings source {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ValueError(f"Settings source must contain a YAML mapping: {path}")
    return dict(loaded)


def safe_model_error(exc: Exception) -> str:
    """A model error safe to show and log: model keys and other secrets removed, at most 1,000 characters."""

    rendered = str(exc)
    for secret in process_model_keys().known_values():
        if secret:
            rendered = rendered.replace(secret, "[REDACTED]")
    redacted = redact_text(rendered).strip()
    if len(redacted) > 1000:
        redacted = f"{redacted[:997]}..."
    return redacted or type(exc).__name__


def _restore_source_bytes(path: Path, *, existed: bool, content: bytes) -> None:
    if existed:
        atomic_write_bytes(path, content)
    else:
        path.unlink(missing_ok=True)


class RuntimeLLMOverrideApplier:
    """Apply one authorized workspace-profile plan as a verified transaction."""

    def __init__(self, agent: Any) -> None:
        self.agent = agent

    def apply(
        self,
        config: Any,
        source_path: Path,
        changes: Sequence[LLMOverrideChange],
        *,
        snapshot: LLMOverrideSourceSnapshot,
        change_id: str,
    ) -> dict[str, Any]:
        previous_existed = source_path.exists()
        previous_bytes = source_path.read_bytes() if previous_existed else b""
        if (
            previous_existed != snapshot.existed
            or sha256_bytes(previous_bytes) != snapshot.sha256
        ):
            raise PermissionError(
                f"Runtime settings source changed after planning: {source_path}. "
                "Stage a fresh plan."
            )

        prepared = self._prepare(config, source_path, changes)
        # Tested again right before writing: the staging test may be minutes
        # old, and the endpoint, key or model may have changed since.
        preflight, failure = self.test_candidates(prepared.candidates, change_id=change_id)
        if failure is not None:
            return failure

        backup_path, failure = self._persist_and_verify(
            source_path,
            prepared,
            previous_existed=previous_existed,
            previous_bytes=previous_bytes,
        )
        if failure is not None:
            return failure
        if backup_path is None:
            raise RuntimeError(
                "LLM persistence completed without a backup path or failure result."
            )

        live_applied, failure = self._activate_current_profile(
            prepared.current_profile,
            prepared.current_paths,
            source_path=source_path,
            previous_existed=previous_existed,
            previous_bytes=previous_bytes,
            backup_path=backup_path,
        )
        if failure is not None:
            return failure

        current_after_bytes = source_path.read_bytes() if source_path.exists() else b""
        prune_config_backups(
            source_path,
            event_logger=LOGGER,
            preserve_paths=(backup_path,),
        )
        capability_follow_up = self._capability_follow_up_for(prepared)
        result: dict[str, Any] = {
            "persistence_target": WORKSPACE_LLM_PERSISTENCE_TARGET,
            "saved_path": str(source_path),
            "backup_path": str(backup_path),
            "previous_existed": previous_existed,
            "previous_sha256": sha256_bytes(previous_bytes),
            "sha256": sha256_bytes(current_after_bytes),
            "live_applied": live_applied,
            "preflight": preflight,
            "persistence_verification": {
                "status": "passed",
                "validated_after_write": True,
                "verified_entries": sorted(prepared.expected_entries),
            },
            "capability_follow_up": capability_follow_up,
        }
        if prepared.default_changes:
            result["workspace_default_llm_override"] = (
                prepared.default_profile.persistence_entry()
            )
        if prepared.current_profile is not None:
            result["llm_override"] = prepared.current_profile.persistence_entry()
        return result

    def prepare_candidates(
        self,
        config: Any,
        source_path: Path,
        changes: Sequence[LLMOverrideChange],
    ) -> list[RuntimeLLMModelCandidate]:
        """Build the same target profiles staging and apply test, without writing."""

        return self._prepare(config, source_path, changes).candidates

    @staticmethod
    def instance_candidates(config: Any, changes: Sequence[LLMOverrideChange]) -> list[RuntimeLLMModelCandidate]:
        """Test the instance baseline with its own endpoint and request settings.

        The baseline is distinct from workspace overrides and the lead's live
        model. Use the runtime's normal settings projection so omitted values
        have exactly the defaults a newly started agent will receive.
        """

        from config_loader import (
            _apply_known_model_capabilities,
            lookup_configured_model_capabilities,
            lookup_configured_model_request_constraints,
        )
        from custom_agents.xai_oauth_auth import is_xai_oauth_provider, resolve_xai_oauth_model_id
        from default_values import LLM_DEFAULTS
        from runtime_settings.projection import _project_model

        paths = frozenset(change.path for change in changes if change.path.startswith(INSTANCE_LLM_PATH_PREFIX))
        if not paths & _PREFLIGHT_PATHS:
            return []
        llm_cfg = config.llm.model_dump(mode="python", exclude_none=True)
        if is_xai_oauth_provider(llm_cfg.get("provider")) and not str(llm_cfg.get("model_id") or "").strip():
            llm_cfg["model_id"] = resolve_xai_oauth_model_id(llm_cfg.get("provider"), None)
        # Startup resolves configured model facts and request constraints before
        # projection. Share those rules, without reading higher-precedence overrides.
        lookup_args = {
            "provider": llm_cfg.get("provider", LLM_DEFAULTS.provider.value),
            "model_id": llm_cfg.get("model_id", LLM_DEFAULTS.model_id.value),
        }
        capabilities = llm_cfg.get("model_capabilities")
        _apply_known_model_capabilities(llm_cfg, lookup_configured_model_capabilities(capabilities, **lookup_args))
        llm_cfg.update(lookup_configured_model_request_constraints(capabilities, **lookup_args))
        settings = _project_model(
            llm_cfg=llm_cfg,
            llm_failover={},
            resolved_llm_override_path=config.layout.llm_overrides_path,
        )
        profile = runtime_llm_profile_from_agent(SimpleNamespace(**settings))
        return [RuntimeLLMModelCandidate("the team baseline", profile, paths, settings)]

    @staticmethod
    def _load_override_sections(
        source_path: Path,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        payload = _read_yaml_mapping(source_path)
        existing_default = payload.get("default")
        if not isinstance(existing_default, dict):
            existing_default = {}
            payload["default"] = existing_default
        agent_types = payload.get("agent_types")
        if not isinstance(agent_types, dict):
            agent_types = {}
            payload["agent_types"] = agent_types
        return payload, existing_default, agent_types

    def _partition_override_changes(
        self,
        changes: Sequence[LLMOverrideChange],
    ) -> tuple[
        dict[str, Any],
        set[str],
        dict[str, dict[str, Any]],
        dict[str, set[str]],
    ]:
        default_changes: dict[str, Any] = {}
        default_paths: set[str] = set()
        changes_by_type: dict[str, dict[str, Any]] = {}
        paths_by_type: dict[str, set[str]] = {}
        for change in changes:
            field_name = self._entry_field(change)
            if change.path.startswith(WORKSPACE_DEFAULT_LLM_PATH_PREFIX):
                default_changes[field_name] = change.value
                default_paths.add(change.path)
                continue
            agent_type = str(change.target_agent_type or "").strip()
            changes_by_type.setdefault(agent_type, {})[field_name] = change.value
            paths_by_type.setdefault(agent_type, set()).add(change.path)
        return default_changes, default_paths, changes_by_type, paths_by_type

    def _prepare_agent_type_entry(
        self,
        config: Any,
        *,
        payload: Mapping[str, Any],
        agent_types: Mapping[str, Any],
        agent_type: str,
        current_type: str,
        default_changed: bool,
        staged_values: Mapping[str, Any],
    ) -> tuple[dict[str, Any], RuntimeLLMProfile]:
        existing_entry = agent_types.get(agent_type)
        entry = dict(existing_entry) if isinstance(existing_entry, dict) else {}
        inherited_profile = self._profile_for_agent_type(
            config,
            payload=payload,
            agent_type=agent_type,
            existing_entry=existing_entry,
            use_live_profile=(agent_type == current_type and not default_changed),
        )
        candidate_profile = self._profile_with_staged_values(
            inherited_profile,
            staged_values,
        )
        if agent_type == current_type:
            entry.update(candidate_profile.persistence_entry())
        else:
            self._clear_stale_model_fields(
                entry,
                staged_values=staged_values,
                previous_profile=inherited_profile,
            )
            entry.update(staged_values)
        return entry, candidate_profile

    def _prepare(
        self,
        config: Any,
        source_path: Path,
        changes: Sequence[LLMOverrideChange],
    ) -> _PreparedOverrides:
        from config_loader import validate_llm_override_payload

        payload, existing_default, agent_types = self._load_override_sections(
            source_path
        )

        current_type = str(
            getattr(self.agent, "named_agent_type", None)
            or getattr(self.agent, "name", None)
            or TEAM_ARCHITECT_AGENT_TYPE
        ).strip()
        (
            default_changes,
            default_paths,
            changes_by_type,
            paths_by_type,
        ) = self._partition_override_changes(changes)

        expected_entries: dict[str, dict[str, Any]] = {}
        default_profile = self._effective_profile(
            config,
            default_entry=existing_default,
        )
        candidates: list[RuntimeLLMModelCandidate] = []
        if default_changes:
            default_profile = self._profile_with_staged_values(
                default_profile,
                default_changes,
            )
            default_entry = default_profile.persistence_entry()
            payload["default"] = default_entry
            expected_entries["default"] = default_entry
            candidates.append(
                RuntimeLLMModelCandidate(
                    target="default",
                    profile=default_profile,
                    paths=frozenset(default_paths),
                )
            )

        current_profile: RuntimeLLMProfile | None = None
        for agent_type, staged_values in changes_by_type.items():
            entry, candidate_profile = self._prepare_agent_type_entry(
                config,
                payload=payload,
                agent_types=agent_types,
                agent_type=agent_type,
                current_type=current_type,
                default_changed=bool(default_changes),
                staged_values=staged_values,
            )
            if agent_type == current_type:
                current_profile = candidate_profile
            agent_types[agent_type] = entry
            expected_entries[f"agent_types.{agent_type}"] = dict(entry)
            candidates.append(
                RuntimeLLMModelCandidate(
                    target=agent_type,
                    profile=candidate_profile,
                    paths=frozenset(paths_by_type[agent_type]),
                )
            )

        validation = validate_llm_override_payload(payload)
        if not validation.get("valid"):
            errors = "; ".join(str(error) for error in validation.get("errors") or [])
            raise ValueError(f"LLM override file validation failed before write: {errors}")
        return _PreparedOverrides(
            payload=payload,
            new_bytes=yaml.safe_dump(payload, sort_keys=False).encode("utf-8"),
            expected_entries=expected_entries,
            default_changes=default_changes,
            default_paths=default_paths,
            default_profile=default_profile,
            current_profile=current_profile,
            current_paths=paths_by_type.get(current_type, set()),
            candidates=candidates,
        )

    def _profile_for_agent_type(
        self,
        config: Any,
        *,
        payload: Mapping[str, Any],
        agent_type: str,
        existing_entry: Any,
        use_live_profile: bool,
    ) -> RuntimeLLMProfile:
        if use_live_profile:
            return runtime_llm_profile_from_agent(self.agent)
        return self._effective_profile(
            config,
            default_entry=payload.get("default"),
            agent_type=agent_type,
            agent_entry=existing_entry,
        )

    @staticmethod
    def _clear_stale_model_fields(
        entry: dict[str, Any],
        *,
        staged_values: Mapping[str, Any],
        previous_profile: RuntimeLLMProfile,
    ) -> None:
        model_changed = any(
            field in staged_values
            and staged_values[field] != getattr(previous_profile, field)
            for field in ("provider", "model_id")
        )
        if model_changed:
            for field in stale_model_fields(
                previous_provider=previous_profile.provider,
                candidate_provider=staged_values.get("provider", previous_profile.provider),
            ):
                entry[field] = None

    def test_candidates(
        self,
        candidates: list[RuntimeLLMModelCandidate],
        *,
        change_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        probe_results: list[dict[str, Any]] = []
        for candidate in candidates:
            if not candidate.paths & _PREFLIGHT_PATHS:
                continue
            if not hasattr(self.agent, "probe_runtime_llm_profile"):
                raise RuntimeError(
                    "Runtime model profile testing is unavailable; refusing to "
                    "write an untested model-affecting change."
                )
            try:
                kwargs = {"request_settings": candidate.request_settings} if candidate.request_settings is not None else {}
                result = self.agent.probe_runtime_llm_profile(candidate.profile, **kwargs)
                probe_results.append({"target": candidate.target, **result})
            except Exception as exc:
                safe_error = self._safe_error(exc)
                LOGGER.warning(
                    "Runtime LLM profile preflight rejected the candidate; active and "
                    "persisted profiles were left unchanged. "
                    "event=runtime_llm_profile_preflight_failed change_id=%s "
                    "agent_id=%s target=%s candidate_provider=%s "
                    "candidate_model_id=%s candidate_litellm_api_mode=%s error=%s",
                    change_id,
                    getattr(self.agent, "id", "unknown"),
                    candidate.target,
                    candidate.profile.provider,
                    candidate.profile.model_id,
                    candidate.profile.litellm_api_mode,
                    safe_error,
                )
                return {}, {
                    "error": safe_error,
                    "phase": "preflight",
                    "candidate": {
                        "target": candidate.target,
                        "provider": candidate.profile.provider,
                        "model_id": candidate.profile.model_id,
                        "litellm_api_mode": candidate.profile.litellm_api_mode,
                    },
                    "active_selection_unchanged": True,
                    "persisted_selection_unchanged": True,
                }
        if not probe_results:
            return {
                "status": "skipped",
                "reason": "profile_change_does_not_require_provider_probe",
                "tested_before_write": False,
                "profiles": [],
            }, None
        preflight: dict[str, Any] = {
            "status": "passed",
            "tested_before_write": True,
            "profiles": probe_results,
        }
        if len(probe_results) == 1:
            preflight.update(probe_results[0])
        return preflight, None

    def _persist_and_verify(
        self,
        source_path: Path,
        prepared: _PreparedOverrides,
        *,
        previous_existed: bool,
        previous_bytes: bytes,
    ) -> tuple[Path | None, dict[str, Any] | None]:
        current_exists = source_path.exists()
        current_bytes = source_path.read_bytes() if current_exists else b""
        if current_exists != previous_existed or current_bytes != previous_bytes:
            LOGGER.warning(
                "Runtime LLM profile persistence rejected a concurrent source edit. "
                "event=runtime_llm_profile_persistence_failed agent_id=%s override_path=%s",
                getattr(self.agent, "id", "unknown"),
                source_path,
            )
            return None, {
                "error": (
                    "LLM override file changed after the runtime settings plan was "
                    "prepared; no changes were written. Stage the change again."
                ),
                "phase": "persistence_commit",
                "active_selection_unchanged": True,
                "persistence_write_applied": False,
            }

        backup_path = source_path.with_name(
            f"{source_path.name}.bak.runtime-settings."
            f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}"
        )
        atomic_write_bytes(backup_path, previous_bytes)
        try:
            atomic_write_bytes(source_path, prepared.new_bytes)
        except Exception as exc:
            safe_error = self._safe_error(exc)
            LOGGER.warning(
                "Runtime LLM profile persistence failed after preflight. "
                "event=runtime_llm_profile_persistence_failed agent_id=%s "
                "override_path=%s error=%s",
                getattr(self.agent, "id", "unknown"),
                source_path,
                safe_error,
            )
            return None, {
                "error": safe_error,
                "phase": "persistence_commit",
                "active_selection_unchanged": True,
                "persistence_write_applied": False,
                "backup_path": str(backup_path),
            }

        try:
            self._verify_persisted(source_path, prepared.expected_entries)
        except Exception as exc:
            safe_error = self._safe_error(exc)
            _restore_source_bytes(
                source_path,
                existed=previous_existed,
                content=previous_bytes,
            )
            LOGGER.error(
                "Runtime LLM profile persistence verification failed; restored the prior source. "
                "event=runtime_llm_profile_persistence_verification_failed "
                "agent_id=%s override_path=%s error=%s",
                getattr(self.agent, "id", "unknown"),
                source_path,
                safe_error,
            )
            return None, {
                "error": safe_error,
                "phase": "persistence_verification",
                "active_selection_unchanged": True,
                "persisted_selection_unchanged": True,
                "backup_path": str(backup_path),
            }
        return backup_path, None

    @staticmethod
    def _verify_persisted(
        source_path: Path,
        expected_entries: Mapping[str, dict[str, Any]],
    ) -> None:
        from config_loader import validate_llm_override_payload

        payload = _read_yaml_mapping(source_path, must_exist=True)
        validation = validate_llm_override_payload(payload)
        if not validation.get("valid"):
            raise ValueError(
                "Persisted LLM override failed validation: "
                + "; ".join(str(error) for error in validation.get("errors") or [])
            )
        for entry_path, expected_entry in expected_entries.items():
            observed_entry = (
                payload.get("default")
                if entry_path == "default"
                else (payload.get("agent_types") or {}).get(
                    entry_path.removeprefix("agent_types.")
                )
            )
            if observed_entry != expected_entry:
                raise ValueError(
                    f"Persisted LLM override verification failed for {entry_path}."
                )

    def _activate_current_profile(
        self,
        profile: RuntimeLLMProfile | None,
        current_paths: set[str],
        *,
        source_path: Path,
        previous_existed: bool,
        previous_bytes: bytes,
        backup_path: Path,
    ) -> tuple[list[str], dict[str, Any] | None]:
        if profile is None:
            return [], None
        try:
            self._apply_live_profile(profile)
            if runtime_llm_profile_from_agent(self.agent) != profile:
                raise RuntimeError(
                    "Live model profile verification did not match the persisted candidate."
                )
            return sorted(current_paths), None
        except Exception as exc:
            safe_error = self._safe_error(exc)
            rollback_error: str | None = None
            try:
                _restore_source_bytes(
                    source_path,
                    existed=previous_existed,
                    content=previous_bytes,
                )
            except Exception as rollback_exc:
                rollback_error = self._safe_error(rollback_exc)
                LOGGER.exception(
                    "Runtime LLM activation and persistence rollback both failed. "
                    "event=runtime_llm_profile_rollback_failed agent_id=%s "
                    "override_path=%s",
                    getattr(self.agent, "id", None),
                    source_path,
                )
            LOGGER.error(
                "Runtime LLM profile activation failed. "
                "event=runtime_llm_profile_activation_failed agent_id=%s "
                "candidate_provider=%s candidate_model_id=%s "
                "persisted_rolled_back=%s error=%s rollback_error=%s",
                getattr(self.agent, "id", "unknown"),
                profile.provider,
                profile.model_id,
                rollback_error is None,
                safe_error,
                rollback_error,
            )
            return [], {
                "error": safe_error,
                "phase": "live_commit",
                "candidate": {
                    "provider": profile.provider,
                    "model_id": profile.model_id,
                },
                "active_selection_unchanged": True,
                "persisted_selection_unchanged": rollback_error is None,
                "rollback_error": rollback_error,
                "backup_path": str(backup_path),
            }

    def _apply_live_profile(self, profile: RuntimeLLMProfile) -> None:
        if hasattr(self.agent, "apply_runtime_llm_profile"):
            self.agent.apply_runtime_llm_profile(
                profile,
                source="user_runtime_settings_change",
            )
            return
        if hasattr(self.agent, "apply_runtime_llm_override"):
            self.agent.apply_runtime_llm_override(
                profile.provider,
                profile.model_id,
                reasoning_effort=profile.reasoning_effort,
                reasoning_summary=profile.reasoning_summary,
                litellm_api_mode=profile.litellm_api_mode,
                context_window_tokens=profile.context_window_tokens,
                supports_image_input=profile.supports_image_input,
                supports_audio_input=profile.supports_audio_input,
                supports_file_input=profile.supports_file_input,
                source="user_runtime_settings_change",
            )

    def _safe_error(self, exc: Exception) -> str:
        return safe_model_error(exc)

    def agent_type_profile(self, config: Any, payload: Mapping[str, Any], agent_type: str) -> RuntimeLLMProfile:
        """The model profile an agent type runs with: instance configuration, then LLM_OVERRIDES.yaml."""

        return self._effective_profile(
            config,
            default_entry=payload.get("default"),
            agent_type=agent_type,
            agent_entry=(payload.get("agent_types") or {}).get(agent_type),
        )

    @staticmethod
    def _profile_values_from_entry(
        values: dict[str, Any],
        entry: Any,
    ) -> dict[str, Any]:
        if not isinstance(entry, Mapping):
            return values
        merged = dict(values)
        provider = entry.get("provider") or entry.get("model_provider")
        if str(provider or "").strip():
            merged["provider"] = str(provider).strip()
        model_id = entry.get("model_id")
        if str(model_id or "").strip():
            merged["model_id"] = str(model_id).strip()
        for field in (*MODEL_TUNING_FIELDS, *MODEL_CAPABILITY_FIELDS):
            if field in entry:
                merged[field] = entry.get(field)
        return merged

    def _effective_profile(
        self,
        config: Any,
        *,
        default_entry: Any,
        agent_type: str | None = None,
        agent_entry: Any = None,
    ) -> RuntimeLLMProfile:
        values = {
            field: getattr(config.llm, field, None) for field in MODEL_PROFILE_FIELDS
        }
        if agent_type:
            target_settings = config.agent_types.get(agent_type)
            target_llm = (
                getattr(target_settings, "llm", None)
                if target_settings is not None
                else None
            )
            if target_llm is not None:
                for field in MODEL_PROFILE_FIELDS:
                    value = getattr(target_llm, field, None)
                    if value is not None:
                        values[field] = value
        values = self._profile_values_from_entry(values, default_entry)
        values = self._profile_values_from_entry(values, agent_entry)
        return RuntimeLLMProfile.model_validate(values)

    @staticmethod
    def _profile_with_staged_values(
        profile: RuntimeLLMProfile,
        staged_values: Mapping[str, Any],
    ) -> RuntimeLLMProfile:
        values = profile.model_dump(mode="python")
        model_changed = any(
            field in staged_values and staged_values[field] != getattr(profile, field)
            for field in ("provider", "model_id")
        )
        if model_changed:
            for field in stale_model_fields(
                previous_provider=profile.provider,
                candidate_provider=staged_values.get("provider", profile.provider),
            ):
                values[field] = None
        values.update(staged_values)
        return RuntimeLLMProfile.model_validate(values)

    @staticmethod
    def _entry_field(change: LLMOverrideChange) -> str:
        if change.path.startswith(CURRENT_AGENT_LLM_PATH_PREFIX):
            return change.path.removeprefix(CURRENT_AGENT_LLM_PATH_PREFIX)
        if change.path.startswith(WORKSPACE_DEFAULT_LLM_PATH_PREFIX):
            return change.path.removeprefix(WORKSPACE_DEFAULT_LLM_PATH_PREFIX)
        raise ValueError(f"Unsupported workspace LLM setting path: {change.path}")

    @staticmethod
    def _capability_follow_up(
        changed_paths: set[str],
        profile: RuntimeLLMProfile,
    ) -> dict[str, Any] | None:
        missing_fields = [
            field for field in MODEL_CAPABILITY_FIELDS if getattr(profile, field) is None
        ]
        model_switched = bool(
            {
                "current_agent.llm.provider",
                "current_agent.llm.model_id",
                "workspace_default.llm.provider",
                "workspace_default.llm.model_id",
            }
            & changed_paths
        )
        if not missing_fields or not model_switched:
            return None
        return {
            "reason": "model_capability_metadata_unknown",
            "provider": profile.provider,
            "model_id": profile.model_id,
            "missing_fields": missing_fields,
            "recommended_agent_type": "InternalImprovementAgent",
            "recommended_tool": "lookup_model_capability_candidates",
            "approval_required": True,
            "user_feedback": (
                "Present the lookup evidence and stable user_feedback_options as a "
                "multiple-choice question before writing capability metadata."
            ),
        }

    def _capability_follow_up_for(
        self,
        prepared: _PreparedOverrides,
    ) -> dict[str, Any] | None:
        if prepared.default_changes:
            return self._capability_follow_up(
                prepared.default_paths,
                prepared.default_profile,
            )
        if prepared.current_profile is not None:
            return self._capability_follow_up(
                prepared.current_paths,
                prepared.current_profile,
            )
        return None
