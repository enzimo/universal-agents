"""Model-provider failover policy and durable state helpers."""

from __future__ import annotations

import logging

import json
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from default_values import LLM_DEFAULTS
from runtime_settings.values import deep_merge_dicts
from config_loader import (
    load_persistent_llm_override,
    lookup_configured_model_capabilities,
    lookup_configured_model_request_constraints,
)
from custom_agents.endpoint_refresh import refresh_team_endpoint
from custom_agents.model_keys import last_call_used_personal_key
from custom_agents.person_model_runtime import person_model_active
from custom_agents.provider_error_runtime import is_model_key_error, is_provider_failover_eligible_error
from custom_agents.runtime_paths import agent_layout
from runtime_settings.model_request_options import (
    LITELLM_API_MODE_CHAT_COMPLETIONS,
    normalize_litellm_api_mode,
    normalize_reasoning_summary,
)
from custom_messages.enums import (
    ProgressState,
    TaskActivityStage,
    TaskActivityType,
    TaskHistoryImportance,
    TaskHistoryType,
)

try:  # pragma: no cover - import location is SDK-version dependent.
    from strands.types.exceptions import ModelThrottledException
except Exception:  # pragma: no cover
    ModelThrottledException = ()  # type: ignore[assignment]

try:  # pragma: no cover - fcntl is available on supported Unix deployments.
    import fcntl
except Exception:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


_SELECTION_REASON_PROVIDER_FAILOVER = "provider_failover"
_SELECTION_REASON_CONFIGURED_PRIMARY_CHANGED = "configured_primary_changed"


@dataclass(frozen=True)
class ModelCandidate:
    provider: str
    model_id: str
    reasoning_effort: Optional[str] = None
    reasoning_summary: Optional[str] = None
    litellm_api_mode: str = LITELLM_API_MODE_CHAT_COMPLETIONS
    context_window_tokens: Optional[int] = None
    supports_image_input: Optional[bool] = None
    supports_audio_input: Optional[bool] = None
    supports_file_input: Optional[bool] = None
    # Only the primary carries saved field presence. An explicit null must
    # clear the current value; an absent field can keep it between turns.
    overridden_fields: frozenset[str] = frozenset()

    @property
    def key(self) -> str:
        return f"{self.provider}/{self.model_id}"

    def capability_kwargs(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        if self.supports_image_input is not None:
            result["supports_image_input"] = self.supports_image_input
        if self.supports_audio_input is not None:
            result["supports_audio_input"] = self.supports_audio_input
        if self.supports_file_input is not None:
            result["supports_file_input"] = self.supports_file_input
        return result

    def runtime_profile_kwargs(self) -> dict[str, Any]:
        return {
            "reasoning_effort": self.reasoning_effort,
            "reasoning_summary": self.reasoning_summary,
            "litellm_api_mode": self.litellm_api_mode,
            "context_window_tokens": self.context_window_tokens,
            "supports_image_input": self.supports_image_input,
            "supports_audio_input": self.supports_audio_input,
            "supports_file_input": self.supports_file_input,
        }


@dataclass(frozen=True)
class ModelFailoverOutcome:
    from_candidate: ModelCandidate
    to_candidate: ModelCandidate
    notice: str
    retry_inline: bool
    requeue_required: bool
    selection_reason: str = _SELECTION_REASON_PROVIDER_FAILOVER


class ProviderFailoverRequeued(RuntimeError):
    """Raised after a provider switch requeues the current task for recovery."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse_timestamp(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _as_positive_int(value: Any, default: int) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _as_positive_float(value: Any, default: float) -> float:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return default


def _config(agent: Any) -> Dict[str, Any]:
    configured = getattr(agent, "llm_failover", None)
    return deep_merge_dicts(
        dict(LLM_DEFAULTS.failover.value),
        dict(configured or {}) if isinstance(configured, dict) else {},
    )


def is_enabled(agent: Any) -> bool:
    return bool(_config(agent).get("enabled"))


def should_notify_user(agent: Any) -> bool:
    """Return whether provider switches should produce user-visible notices."""

    return bool(_config(agent).get("notify_user", True))


def is_provider_failover_outcome(outcome: ModelFailoverOutcome) -> bool:
    """Return whether an outcome is a real fallback switch."""

    return outcome.selection_reason == _SELECTION_REASON_PROVIDER_FAILOVER


def _state_path(agent: Any, config: Dict[str, Any]) -> Path:
    """state/model_failover/<team>_<type>.json: a runtime record agents cannot open."""

    del config
    identity = "_".join(
        part
        for part in (
            getattr(agent, "team_id", None),
            getattr(agent, "named_agent_type", None),
        )
        if part
    )
    safe_identity = re.sub(r"[^A-Za-z0-9_.-]+", "_", identity or "agent")
    return agent_layout(agent).model_failover_dir / f"{safe_identity}.json"


@contextmanager
def _state_file_lock(agent: Any, config: Dict[str, Any]):
    if fcntl is None:
        yield
        return
    path = _state_path(agent, config)
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def _load_state(agent: Any, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cfg = config or _config(agent)
    path = _state_path(agent, cfg)
    try:
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                raw.setdefault("schema_version", 1)
                raw.setdefault("providers", {})
                return raw
    except Exception as exc:
        getattr(agent, "_logger", None).warning(
            "[%s] Failed to read model failover state from %s: %s",
            getattr(agent, "id", "unknown"),
            path,
            exc,
        )
    return {"schema_version": 1, "providers": {}}


def _save_state(
    agent: Any, state: Dict[str, Any], config: Optional[Dict[str, Any]] = None
) -> None:
    cfg = config or _config(agent)
    path = _state_path(agent, cfg)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps(state, indent=2, sort_keys=True), encoding="utf-8"
        )
        tmp_path.replace(path)
    except Exception as exc:
        getattr(agent, "_logger", None).warning(
            "[%s] Failed to persist model failover state to %s: %s",
            getattr(agent, "id", "unknown"),
            path,
            exc,
        )


def _persistent_override(agent: Any) -> dict[str, Any] | None:
    """Refresh per-model facts without replacing an active fallback selection."""

    resolved = load_persistent_llm_override(
        instance_root=getattr(agent, "instance_root", None),
        explicit_override_path=getattr(agent, "llm_override_path", None),
        agent_type=getattr(agent, "named_agent_type", None),
    )
    agent._persistent_model_capabilities = resolved.get("model_capabilities") if resolved else None
    return resolved


def _primary_candidate(agent: Any) -> ModelCandidate:
    resolved = _persistent_override(agent)
    provider = (
        resolved.get("model_provider")
        if resolved and resolved.get("model_provider")
        else (
            getattr(agent, "_configured_model_provider", None)
            or getattr(agent, "model_provider", None)
        )
    )
    model_id = (
        resolved.get("model_id")
        if resolved and resolved.get("model_id")
        else (
            getattr(agent, "_configured_model_id", None)
            or getattr(agent, "model_id", None)
        )
    )
    return ModelCandidate(
        str(provider or LLM_DEFAULTS.provider.value),
        str(model_id or LLM_DEFAULTS.model_id.value),
        reasoning_effort=(
            str(resolved.get("reasoning_effort") or "").strip() or None
            if resolved and "reasoning_effort" in resolved
            else str(getattr(agent, "reasoning_effort", "") or "").strip() or None
        ),
        reasoning_summary=(
            normalize_reasoning_summary(resolved.get("reasoning_summary"))
            if resolved and "reasoning_summary" in resolved
            else normalize_reasoning_summary(getattr(agent, "reasoning_summary", None))
        ),
        litellm_api_mode=(
            normalize_litellm_api_mode(resolved.get("litellm_api_mode"))
            if resolved and "litellm_api_mode" in resolved
            else normalize_litellm_api_mode(getattr(agent, "litellm_api_mode", None))
        ),
        context_window_tokens=(
            _optional_context_window(resolved.get("context_window_tokens"))
            if resolved and "context_window_tokens" in resolved
            else None
        ),
        supports_image_input=(
            _optional_bool(resolved.get("supports_image_input"))
            if resolved and "supports_image_input" in resolved
            else None
        ),
        supports_audio_input=(
            _optional_bool(resolved.get("supports_audio_input"))
            if resolved and "supports_audio_input" in resolved
            else None
        ),
        supports_file_input=(
            _optional_bool(resolved.get("supports_file_input"))
            if resolved and "supports_file_input" in resolved
            else None
        ),
        overridden_fields=frozenset(resolved or {}),
    )


def _optional_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return None
    lowered = str(value).strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    return None


def _optional_context_window(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 1024 else None


def _fallback_candidates(config: Dict[str, Any]) -> List[ModelCandidate]:
    candidates: List[ModelCandidate] = []
    for raw in config.get("fallbacks") or []:
        if not isinstance(raw, dict):
            continue
        provider = str(raw.get("provider") or raw.get("model_provider") or "").strip()
        model_id = str(raw.get("model_id") or "").strip()
        if provider and model_id:
            candidates.append(
                ModelCandidate(
                    provider,
                    model_id,
                    reasoning_effort=str(raw.get("reasoning_effort") or "").strip()
                    or None,
                    reasoning_summary=normalize_reasoning_summary(
                        raw.get("reasoning_summary")
                    ),
                    litellm_api_mode=normalize_litellm_api_mode(
                        raw.get("litellm_api_mode")
                    ),
                    context_window_tokens=_optional_context_window(
                        raw.get("context_window_tokens")
                    ),
                    supports_image_input=_optional_bool(
                        raw.get("supports_image_input")
                    ),
                    supports_audio_input=_optional_bool(
                        raw.get("supports_audio_input")
                    ),
                    supports_file_input=_optional_bool(raw.get("supports_file_input")),
                )
            )
    return candidates


def _candidate_list(agent: Any, config: Dict[str, Any]) -> List[ModelCandidate]:
    ordered = [_primary_candidate(agent), *_fallback_candidates(config)]
    seen: set[str] = set()
    result: List[ModelCandidate] = []
    for candidate in ordered:
        if candidate.key in seen:
            continue
        seen.add(candidate.key)
        result.append(candidate)
    return result


def _current_candidate(agent: Any) -> ModelCandidate:
    return ModelCandidate(
        str(getattr(agent, "model_provider", "")),
        str(getattr(agent, "model_id", "")),
        reasoning_effort=str(getattr(agent, "reasoning_effort", "") or "").strip()
        or None,
        reasoning_summary=normalize_reasoning_summary(
            getattr(agent, "reasoning_summary", None)
        ),
        litellm_api_mode=normalize_litellm_api_mode(
            getattr(agent, "litellm_api_mode", None)
        ),
        context_window_tokens=_optional_context_window(
            getattr(agent, "context_window_tokens", None)
        ),
        supports_image_input=_optional_bool(
            getattr(agent, "supports_image_input", None)
        ),
        supports_audio_input=_optional_bool(
            getattr(agent, "supports_audio_input", None)
        ),
        supports_file_input=_optional_bool(getattr(agent, "supports_file_input", None)),
    )


def _candidate_capability_kwargs(
    agent: Any, candidate: ModelCandidate
) -> dict[str, Any]:
    result = candidate.runtime_profile_kwargs()
    configured_capabilities = lookup_configured_model_capabilities(
        getattr(agent, "model_capabilities", None),
        provider=candidate.provider,
        model_id=candidate.model_id,
    )
    for field in (
        "supports_image_input",
        "supports_audio_input",
        "supports_file_input",
    ):
        if result.get(field) is None and field in configured_capabilities:
            result[field] = configured_capabilities[field]

    resolved = _persistent_override(agent)
    capabilities = lookup_configured_model_capabilities(
        resolved.get("model_capabilities") if resolved else None,
        provider=candidate.provider,
        model_id=candidate.model_id,
    )
    for field in (
        "supports_image_input",
        "supports_audio_input",
        "supports_file_input",
    ):
        if field in capabilities and getattr(candidate, field) is None:
            result[field] = capabilities[field]

    configured_request_constraints = lookup_configured_model_request_constraints(
        getattr(agent, "model_capabilities", None),
        provider=candidate.provider,
        model_id=candidate.model_id,
    )
    request_constraints = lookup_configured_model_request_constraints(
        resolved.get("model_capabilities") if resolved else None,
        provider=candidate.provider,
        model_id=candidate.model_id,
    )
    result.update(configured_request_constraints)
    result.update(request_constraints)
    constrained_request_fields = {
        *configured_request_constraints,
        *request_constraints,
    }

    current = _current_candidate(agent)
    if candidate.key == current.key:
        for field, value in current.runtime_profile_kwargs().items():
            if (
                field not in constrained_request_fields
                and field not in candidate.overridden_fields
                and result.get(field) is None
            ):
                result[field] = value
    return result


def _provider_state(state: Dict[str, Any], candidate: ModelCandidate) -> Dict[str, Any]:
    providers = state.setdefault("providers", {})
    provider_state = providers.setdefault(candidate.key, {})
    provider_state.setdefault("failures", [])
    return provider_state


def _is_demoted(
    state: Dict[str, Any], candidate: ModelCandidate, now: datetime
) -> bool:
    demoted_until = _parse_timestamp(
        _provider_state(state, candidate).get("demoted_until")
    )
    return bool(demoted_until and demoted_until > now)


def _valid_active_lease(
    state: Dict[str, Any], now: datetime
) -> Optional[ModelCandidate]:
    lease = state.get("active_lease")
    if not isinstance(lease, dict):
        return None
    provider = str(lease.get("provider") or "").strip()
    model_id = str(lease.get("model_id") or "").strip()
    if not provider or not model_id:
        return None
    turns_remaining = _as_positive_int(lease.get("turns_remaining"), 0)
    expires_at = _parse_timestamp(lease.get("expires_at"))
    if turns_remaining > 0 or (expires_at is not None and expires_at > now):
        return ModelCandidate(
            provider,
            model_id,
            reasoning_effort=str(lease.get("reasoning_effort") or "").strip() or None,
            reasoning_summary=normalize_reasoning_summary(
                lease.get("reasoning_summary")
            ),
            litellm_api_mode=normalize_litellm_api_mode(lease.get("litellm_api_mode")),
            context_window_tokens=_optional_context_window(
                lease.get("context_window_tokens")
            ),
            supports_image_input=_optional_bool(lease.get("supports_image_input")),
            supports_audio_input=_optional_bool(lease.get("supports_audio_input")),
            supports_file_input=_optional_bool(lease.get("supports_file_input")),
        )
    state.pop("active_lease", None)
    return None


def _invalidate_superseded_active_lease(
    state: Dict[str, Any],
    *,
    candidates: List[ModelCandidate],
    primary: ModelCandidate,
) -> tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Remove a lease that no longer belongs to the configured model profile."""

    lease = state.get("active_lease")
    if not isinstance(lease, dict):
        return None, None

    lease_snapshot = dict(lease)
    lease_provider = str(lease.get("provider") or "").strip()
    lease_model_id = str(lease.get("model_id") or "").strip()
    candidate_keys = {candidate.key for candidate in candidates}
    if (
        not lease_provider
        or not lease_model_id
        or f"{lease_provider}/{lease_model_id}" not in candidate_keys
    ):
        state.pop("active_lease", None)
        return "target_removed", lease_snapshot

    recorded_primary_provider = str(lease.get("primary_provider") or "").strip()
    recorded_primary_model_id = str(lease.get("primary_model_id") or "").strip()
    if not recorded_primary_provider or not recorded_primary_model_id:
        state.pop("active_lease", None)
        return "primary_untracked", lease_snapshot
    if (
        recorded_primary_provider != primary.provider
        or recorded_primary_model_id != primary.model_id
    ):
        state.pop("active_lease", None)
        return "primary_changed", lease_snapshot
    return None, None


def _log_lease_invalidation(
    agent: Any,
    *,
    reason: str,
    lease: Optional[Dict[str, Any]],
    primary: ModelCandidate,
    candidates: List[ModelCandidate],
) -> None:
    logger = getattr(agent, "_logger", None)
    if logger is None:
        return
    lease = lease or {}
    logger.warning(
        "[%s] event=model_failover_lease_invalidated reason=%s lease_provider=%s "
        "lease_model_id=%s configured_primary_provider=%s configured_primary_model_id=%s "
        "configured_candidates=%s",
        getattr(agent, "id", "unknown"),
        reason,
        lease.get("provider"),
        lease.get("model_id"),
        primary.provider,
        primary.model_id,
        [candidate.key for candidate in candidates],
    )


def _select_preferred_candidate(
    candidates: List[ModelCandidate],
    state: Dict[str, Any],
    now: datetime,
) -> Optional[ModelCandidate]:
    for candidate in candidates:
        if not _is_demoted(state, candidate, now):
            return candidate
    return candidates[0] if candidates else None


def _select_next_candidate(
    candidates: List[ModelCandidate],
    *,
    current: ModelCandidate,
    state: Dict[str, Any],
    now: datetime,
) -> Optional[ModelCandidate]:
    for candidate in candidates:
        if candidate.key != current.key and not _is_demoted(state, candidate, now):
            return candidate
    for candidate in candidates:
        if candidate.key != current.key:
            return candidate
    return None


def _activate_lease(
    state: Dict[str, Any],
    *,
    target: ModelCandidate,
    source: ModelCandidate,
    primary: ModelCandidate,
    config: Dict[str, Any],
    reason: str,
    now: datetime,
    capabilities: Optional[dict[str, Any]] = None,
) -> None:
    switch_for = (
        config.get("switch_for") if isinstance(config.get("switch_for"), dict) else {}
    )
    min_turns = _as_positive_int(switch_for.get("min_turns"), 3)
    min_seconds = _as_positive_float(switch_for.get("min_seconds"), 900.0)
    state["active_lease"] = {
        "provider": target.provider,
        "model_id": target.model_id,
        "source_provider": source.provider,
        "source_model_id": source.model_id,
        "primary_provider": primary.provider,
        "primary_model_id": primary.model_id,
        "started_at": _iso(now),
        "expires_at": _iso(now + timedelta(seconds=min_seconds))
        if min_seconds > 0
        else None,
        "turns_remaining": min_turns,
        "reason": reason,
    }
    state["active_lease"].update(capabilities or target.runtime_profile_kwargs())


def refresh_runtime_llm_selection(agent: Any) -> None:
    # A switched team endpoint applies here, between turns (custom_agents.endpoint_refresh).
    refresh_team_endpoint(agent)
    cfg = _config(agent)
    if not bool(cfg.get("enabled")):
        state_path = _state_path(agent, cfg)
        if state_path.exists():
            with _state_file_lock(agent, cfg):
                state = _load_state(agent, cfg)
                lease = state.pop("active_lease", None)
                if isinstance(lease, dict):
                    primary = _primary_candidate(agent)
                    _log_lease_invalidation(
                        agent,
                        reason="failover_disabled",
                        lease=lease,
                        primary=primary,
                        candidates=[primary],
                    )
                    _save_state(agent, state, cfg)
        agent.refresh_persistent_llm_override()
        return

    now = _now()
    with _state_file_lock(agent, cfg):
        state = _load_state(agent, cfg)
        candidates = _candidate_list(agent, cfg)
        if not candidates:
            return
        primary = candidates[0]
        invalidation_reason, invalidated_lease = _invalidate_superseded_active_lease(
            state,
            candidates=candidates,
            primary=primary,
        )
        if invalidation_reason is not None:
            _log_lease_invalidation(
                agent,
                reason=invalidation_reason,
                lease=invalidated_lease,
                primary=primary,
                candidates=candidates,
            )
            selected = primary
        else:
            selected = _valid_active_lease(state, now)
        if selected is None:
            selected = _select_preferred_candidate(candidates, state, now)
        if selected is None:
            return
        if selected.key == primary.key:
            # A fallback failure can lease the primary too. Keep that lease's
            # lifetime, but use its current saved profile rather than stale
            # settings captured when the lease began.
            selected = primary
        selected_capabilities = _candidate_capability_kwargs(agent, selected)
        lease = state.get("active_lease")
        if (
            isinstance(lease, dict)
            and lease.get("provider") == selected.provider
            and lease.get("model_id") == selected.model_id
        ):
            lease.update(selected_capabilities)
        _save_state(agent, state, cfg)
    agent.apply_runtime_llm_override(
        selected.provider,
        selected.model_id,
        **selected_capabilities,
        source="model_failover_selection",
    )


def retry_strategy_config(agent: Any) -> Optional[Dict[str, int]]:
    if not is_enabled(agent):
        return None
    retry_cfg = _config(agent).get("same_provider_retry")
    retry_cfg = retry_cfg if isinstance(retry_cfg, dict) else {}
    return {
        "max_attempts": _as_positive_int(retry_cfg.get("max_attempts"), 2),
        "initial_delay": _as_positive_int(retry_cfg.get("initial_delay_seconds"), 2),
        "max_delay": _as_positive_int(retry_cfg.get("max_delay_seconds"), 10),
    }


def _provider_error_summary(error: BaseException, *, max_chars: int = 700) -> str:
    text = " ".join(str(error).split())
    if len(text) > max_chars:
        return text[: max_chars - 3] + "..."
    return text or error.__class__.__name__


def is_failover_eligible_error(agent: Any, error: BaseException) -> bool:
    if is_model_key_error(error):
        return False
    if isinstance(error, ModelThrottledException):
        return True
    return is_provider_failover_eligible_error(error)


def _record_failure(
    state: Dict[str, Any],
    *,
    candidate: ModelCandidate,
    task_id: Optional[str],
    error: BaseException,
    config: Dict[str, Any],
    now: datetime,
) -> None:
    demotion = (
        config.get("demotion") if isinstance(config.get("demotion"), dict) else {}
    )
    window_seconds = _as_positive_float(demotion.get("failure_window_seconds"), 3600.0)
    retention_seconds = _as_positive_float(
        config.get("history_retention_seconds"), 604800.0
    )
    failure_threshold = max(1, _as_positive_int(demotion.get("failure_threshold"), 3))
    demotion_seconds = _as_positive_float(demotion.get("demotion_seconds"), 86400.0)
    provider_state = _provider_state(state, candidate)
    failures = [
        item
        for item in provider_state.get("failures", [])
        if isinstance(item, dict)
        and (_parse_timestamp(item.get("at")) or now)
        >= now - timedelta(seconds=retention_seconds)
    ]
    failures.append(
        {
            "at": _iso(now),
            "task_id": task_id,
            "error": _provider_error_summary(error),
        }
    )
    provider_state["failures"] = failures
    provider_state["last_failure_at"] = _iso(now)
    recent = [
        item
        for item in failures
        if (_parse_timestamp(item.get("at")) or now)
        >= now - timedelta(seconds=window_seconds)
    ]
    if len(recent) >= failure_threshold and demotion_seconds > 0:
        provider_state["demoted_until"] = _iso(
            now + timedelta(seconds=demotion_seconds)
        )
        provider_state["demotion_reason"] = (
            f"{len(recent)} provider failures within {int(window_seconds)} seconds"
        )


async def _emit_switch_notice(
    agent: Any,
    *,
    task_id: Optional[str],
    outcome: ModelFailoverOutcome,
    error: BaseException,
    tool_calls_started: bool,
) -> None:
    selection_changed = (
        outcome.selection_reason == _SELECTION_REASON_CONFIGURED_PRIMARY_CHANGED
    )
    details = {
        "event": (
            "model_selection_superseded_after_provider_error"
            if selection_changed
            else "model_provider_failover_switch"
        ),
        "selection_reason": outcome.selection_reason,
        "from_provider": outcome.from_candidate.provider,
        "from_model_id": outcome.from_candidate.model_id,
        "to_provider": outcome.to_candidate.provider,
        "to_model_id": outcome.to_candidate.model_id,
        "retry_inline": outcome.retry_inline,
        "requeue_required": outcome.requeue_required,
        "tool_calls_started": tool_calls_started,
        "error_preview": _provider_error_summary(error),
    }
    if task_id and getattr(agent, "task_manager", None) is not None:
        await agent.task_manager.operations_runtime.run(
            agent.task_manager.append_history_entry,
            task_id,
            outcome.notice,
            entry_type=TaskHistoryType.ERROR_EVENT,
            importance=TaskHistoryImportance.HIGH,
            agent_name=getattr(agent, "name", None),
            agent_id=getattr(agent, "id", None),
            details=details,
        )
    emit_activity = getattr(agent, "emit_activity_event", None)
    if callable(emit_activity) and should_notify_user(agent):
        try:
            emit_activity(
                activity_type=TaskActivityType.RESUMED,
                stage=TaskActivityStage.RUNNING,
                summary=(
                    "Applying updated model selection."
                    if selection_changed
                    else "Switching model provider."
                ),
                detail=outcome.notice,
                task_id=task_id,
                user_visible=True,
                details=details,
            )
        except Exception:
            pass


async def handle_provider_failure(
    agent: Any,
    *,
    error: BaseException,
    task_id: Optional[str],
    tool_calls_started: bool,
) -> Optional[ModelFailoverOutcome]:
    if not is_enabled(agent) or not is_failover_eligible_error(agent, error):
        return None
    if last_call_used_personal_key() or person_model_active(agent):
        # A person's own key or model hit its limit: switching the whole agent
        # type to a fallback would change everyone's model for their sake.
        logging.getLogger(__name__).warning(
            "A model call with a person's own key or model failed; no team-wide failover. "
            "event=model_failover_skipped_personal_key agent_id=%s task_id=%s error_type=%s",
            getattr(agent, "id", None),
            task_id,
            type(error).__name__,
        )
        return None

    cfg = _config(agent)
    now = _now()
    current = _current_candidate(agent)
    with _state_file_lock(agent, cfg):
        state = _load_state(agent, cfg)
        candidates = _candidate_list(agent, cfg)
        if not candidates:
            return None
        primary = candidates[0]
        invalidation_reason, invalidated_lease = _invalidate_superseded_active_lease(
            state,
            candidates=candidates,
            primary=primary,
        )
        if invalidation_reason is not None:
            _log_lease_invalidation(
                agent,
                reason=invalidation_reason,
                lease=invalidated_lease,
                primary=primary,
                candidates=candidates,
            )
        active_lease = _valid_active_lease(state, now)
        current_is_active_selection = current.key == primary.key or (
            active_lease is not None and active_lease.key == current.key
        )
        if not current_is_active_selection:
            state.pop("active_lease", None)
            target = primary
            target_capabilities = _candidate_capability_kwargs(agent, target)
            _save_state(agent, state, cfg)
            selection_reason = _SELECTION_REASON_CONFIGURED_PRIMARY_CHANGED
        else:
            selection_reason = _SELECTION_REASON_PROVIDER_FAILOVER
            if all(candidate.key == current.key for candidate in candidates):
                _save_state(agent, state, cfg)
                return None

            _record_failure(
                state,
                candidate=current,
                task_id=task_id,
                error=error,
                config=cfg,
                now=now,
            )
            target = _select_next_candidate(
                candidates, current=current, state=state, now=now
            )
            if target is None:
                _save_state(agent, state, cfg)
                return None
            target_capabilities = _candidate_capability_kwargs(agent, target)
            _activate_lease(
                state,
                target=target,
                source=current,
                primary=primary,
                config=cfg,
                reason=_provider_error_summary(error),
                now=now,
                capabilities=target_capabilities,
            )
            _save_state(agent, state, cfg)
    agent.apply_runtime_llm_override(
        target.provider,
        target.model_id,
        **target_capabilities,
        source=(
            "model_selection_superseded_after_provider_error"
            if selection_reason == _SELECTION_REASON_CONFIGURED_PRIMARY_CHANGED
            else "model_failover_after_provider_error"
        ),
    )
    if selection_reason == _SELECTION_REASON_CONFIGURED_PRIMARY_CHANGED:
        notice = (
            f"The configured model changed from {current.key} to {target.key} while the prior request "
            "was in flight. Applying the updated model selection without creating a failover lease."
        )
    else:
        notice = (
            f"The model provider {current.key} could not complete the request. "
            f"Switching to {target.key} for the configured failover window."
        )
    outcome = ModelFailoverOutcome(
        from_candidate=current,
        to_candidate=target,
        notice=notice,
        retry_inline=not tool_calls_started,
        requeue_required=tool_calls_started,
        selection_reason=selection_reason,
    )
    await _emit_switch_notice(
        agent,
        task_id=task_id,
        outcome=outcome,
        error=error,
        tool_calls_started=tool_calls_started,
    )
    return outcome


def record_model_success(agent: Any, *, task_id: Optional[str]) -> None:
    if not is_enabled(agent) or person_model_active(agent):
        # A person's own model says nothing about the team's model or its lease.
        return
    cfg = _config(agent)
    now = _now()
    current = _current_candidate(agent)
    with _state_file_lock(agent, cfg):
        state = _load_state(agent, cfg)
        provider_state = _provider_state(state, current)
        provider_state["last_success_at"] = _iso(now)
        provider_state["success_count"] = (
            int(provider_state.get("success_count") or 0) + 1
        )
        lease = state.get("active_lease")
        if (
            isinstance(lease, dict)
            and lease.get("provider") == current.provider
            and lease.get("model_id") == current.model_id
        ):
            turns_remaining = _as_positive_int(lease.get("turns_remaining"), 0)
            if turns_remaining > 0:
                lease["turns_remaining"] = turns_remaining - 1
            if _valid_active_lease(state, now) is None:
                state.pop("active_lease", None)
        _save_state(agent, state, cfg)


async def requeue_task_after_failover(
    agent: Any,
    *,
    task_id: Optional[str],
    outcome: ModelFailoverOutcome,
    error: BaseException,
) -> bool:
    if not task_id or getattr(agent, "task_manager", None) is None:
        return False
    task_info = await agent.task_manager.operations_runtime.run(
        agent.task_manager.get_task_info, task_id
    )
    if task_info is None or await agent.task_manager.operations_runtime.run(
        agent.task_manager.is_terminal_state, task_info.state
    ):
        return False
    selection_changed = (
        outcome.selection_reason == _SELECTION_REASON_CONFIGURED_PRIMARY_CHANGED
    )
    recovery_kind = (
        "model-selection change" if selection_changed else "model-provider failover"
    )
    directive = (
        f"Runtime {recovery_kind} recovery: the previous model request failed after one or more "
        "tool calls had already occurred. Continue from task history and current runtime state. "
        "Do not repeat completed side effects unless a deliberate follow-up or compensation action is needed. "
        f"Current provider is {outcome.to_candidate.key}. Previous provider was {outcome.from_candidate.key}. "
        f"Failure summary: {_provider_error_summary(error)}."
    )
    await agent.task_manager.operations_runtime.run(
        agent.task_manager.append_runtime_resume_directive, task_id, directive
    )
    await agent.task_manager.operations_runtime.run(
        agent.task_manager.append_history_entry,
        task_id,
        (
            "Updated model selection requeued the task after the superseded request failed following tool activity."
            if selection_changed
            else "Model provider failover requeued the task after a provider error occurred following tool activity."
        ),
        entry_type=TaskHistoryType.ERROR_EVENT,
        importance=TaskHistoryImportance.HIGH,
        agent_name=getattr(agent, "name", None),
        agent_id=getattr(agent, "id", None),
        details={
            "event": (
                "model_selection_change_requeue"
                if selection_changed
                else "model_provider_failover_requeue"
            ),
            "selection_reason": outcome.selection_reason,
            "from_provider": outcome.from_candidate.provider,
            "from_model_id": outcome.from_candidate.model_id,
            "to_provider": outcome.to_candidate.provider,
            "to_model_id": outcome.to_candidate.model_id,
            "error_preview": _provider_error_summary(error),
        },
    )
    now_ts = _iso(_now())
    await agent.task_manager.operations_runtime.run(
        agent.task_manager.update_main_task_state,
        task_id,
        ProgressState.PARTIAL,
        now_ts,
    )
    await agent.task_manager.operations_runtime.run(
        agent.task_manager.enqueue_task, task_id, front=True
    )
    return True
