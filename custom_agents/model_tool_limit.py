"""Raising a model's tool cap (``max_tools_per_request``) when an agent asks and an administrator allows it.

An agent whose work needs more tools at once than its model's cap allows
(``custom_agents.tool_loading``) reaches TeamLead: TeamLead's own ``use_tools``
refusal names the request, and a worker's tells it to report the need to
TeamLead. TeamLead stages the runtime setting ``model.max_tools_per_request``
(``custom_agents.runtime_setting_registry``) with ``plan_runtime_setting_change``,
for its own model or, with ``target_agent_type``, for the model an agent type
runs. The plan names that exact provider and model
(``model_capabilities.<provider>.<model id>``).

The cap is a fact about the model, so it applies to every agent that uses that
model, for everyone's work. That is why the setting is not ``user_authorized``:
with authorization on an administrator approves it in the Inbox (like the
team's model endpoint), and with authorization off the person agrees in chat.
It only raises a cap; lowering one would take tools away from every agent on
that model.

Staging (``check_and_test``), before anyone is asked:

1. A task running on a person's own model (``custom_agents.person_model_runtime``)
   cannot ask: its cap is not the configured model's, and that person's model
   and key are not the team's to test.
2. The value is above the model's current cap.
3. One request with that many tools goes to the model
   (``runtime_model_switch.probe_tool_capacity``); a refusal stages nothing
   (owner's decision 2026-10-10 for model switches: try once, then ask).

Applying it (``ModelToolLimitApplier``) once approved, as one transaction:

1. The override file and the target's model are still the ones the plan was
   made for; otherwise nothing is written and the plan must be staged again.
2. The test request runs again (the approval may be days old), and the file is
   checked once more after it.
3. ``LLM_OVERRIDES.yaml`` is written atomically with a backup, validated, read
   back (the previous file is restored when the value did not stick), and old
   backups are pruned.
4. TeamLead uses the new caps from its next model call (``use_saved_caps``);
   other agents read the file between turns. When the receipt cannot be
   written, the runtime settings service restores the file and calls
   ``use_saved_caps`` again, so the running cap matches the file.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import yaml

from config_backup_retention import prune_config_backups
from custom_agents.governance.authorized_operations import atomic_write_bytes, sha256_bytes
from custom_agents.model_runtime_profile import DEFAULT_MAX_TOOLS_PER_REQUEST, MODEL_TOOL_LIMIT_FIELD
from custom_messages.contracts.access_v2 import summary_line
from custom_messages.contracts.runtime_v1 import TEAM_ARCHITECT_AGENT_TYPE

logger = logging.getLogger(__name__)

MODEL_CAPABILITIES_KEY = "model_capabilities"
PERSISTENCE_TARGET = "model_capability"


@dataclass(frozen=True)
class ModelRef:
    """The provider and model a cap belongs to."""

    provider: str
    model_id: str

    @property
    def actual_path(self) -> str:
        return f"{MODEL_CAPABILITIES_KEY}.{self.provider}.{self.model_id}.{MODEL_TOOL_LIMIT_FIELD}"

    @property
    def label(self) -> str:
        return f"{self.model_id} via {self.provider}"


def model_from_path(actual_path: str) -> ModelRef:
    """The provider and model a plan's ``actual_path`` names (``ModelRef.actual_path``)."""

    prefix = f"{MODEL_CAPABILITIES_KEY}."
    suffix = f".{MODEL_TOOL_LIMIT_FIELD}"
    if not actual_path.startswith(prefix) or not actual_path.endswith(suffix):
        raise ValueError(f"Not a model tool cap path: {actual_path!r}.")
    provider, _, model_id = actual_path[len(prefix) : -len(suffix)].partition(".")
    if not provider or not model_id:
        raise ValueError(f"Not a model tool cap path: {actual_path!r}.")
    return ModelRef(provider, model_id)


def _read_mapping(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a mapping.")
    return payload


def _is_self(agent: Any, agent_type: str | None) -> bool:
    """Whether a plan targets the agent itself (as ``RuntimeSettingsService._current_agent_type`` names it)."""

    current = str(getattr(agent, "named_agent_type", None) or getattr(agent, "name", None) or TEAM_ARCHITECT_AGENT_TYPE)
    return not agent_type or agent_type == current.strip()


def target_model(agent: Any, config: Any, override_path: Path, agent_type: str | None) -> tuple[ModelRef, Any]:
    """The model whose cap a plan raises, with the profile its test request uses.

    For the agent itself, the model its calls use now (a fallback when one is
    selected), matching ``tool_loading.tool_limit``; never a person's own
    model (module docstring, staging step 1). For another agent type, the
    model that type is configured with.
    """

    from custom_agents.model_runtime_profile import runtime_llm_profile_from_agent
    from custom_agents.person_model_runtime import active_bundle
    from custom_agents.runtime_settings_llm import RuntimeLLMOverrideApplier

    if _is_self(agent, agent_type):
        bundle = active_bundle(agent)
        if bundle is not None:
            raise ValueError(
                f"This task runs on a person's own model ({bundle.profile.model_id} via {bundle.provider}), so its "
                "tool cap cannot be raised this way. Finish with fewer tools loaded at once, or tell the person "
                "that an administrator can set max_tools_per_request for that model under model_capabilities in "
                "LLM_OVERRIDES.yaml."
            )
        profile = runtime_llm_profile_from_agent(agent)
    else:
        profile = RuntimeLLMOverrideApplier(agent).agent_type_profile(config, _read_mapping(override_path), agent_type)
    provider = str(profile.provider or "").strip()
    model_id = str(profile.model_id or "").strip()
    if not provider or not model_id:
        raise ValueError(f"{agent_type or 'This agent'} has no configured model, so its tool cap cannot be set.")
    return ModelRef(provider, model_id), profile


def effective_tool_limit(config: Any, override_path: Path, model: ModelRef) -> int:
    """The model's cap now: LLM_OVERRIDES.yaml, then the instance configuration, then the default."""

    from config_loader import lookup_configured_model_tool_limit

    payload = _read_mapping(override_path)
    for source in (payload.get(MODEL_CAPABILITIES_KEY), getattr(getattr(config, "llm", None), "model_capabilities", None)):
        limit = lookup_configured_model_tool_limit(source, provider=model.provider, model_id=model.model_id)
        if limit is not None:
            return limit
    return DEFAULT_MAX_TOOLS_PER_REQUEST


def use_saved_caps(agent: Any, override_path: Path) -> None:
    """Make the running agent's caps match LLM_OVERRIDES.yaml (read by ``tool_loading.tool_limit``)."""

    agent._persistent_model_capabilities = _read_mapping(override_path).get(MODEL_CAPABILITIES_KEY)


def _test_request(agent: Any, profile: Any, limit: int) -> dict[str, Any]:
    from custom_agents.runtime_model_switch import probe_tool_capacity

    candidate = agent._configure_model_for_profile(profile)
    timeout = float(getattr(agent, "request_timeout_seconds", None) or 120.0)
    return probe_tool_capacity(candidate, tool_count=limit, timeout_seconds=timeout)


def check_and_test(agent: Any, config: Any, override_path: Path, change: Any) -> dict[str, Any]:
    """Staging checks (module docstring): a raise above the current cap that the model accepts.

    Raises ``ValueError`` with a message for the agent; nothing is staged then.
    """

    from custom_agents.runtime_settings_llm import safe_model_error

    model, profile = target_model(agent, config, override_path, change.target_agent_type)
    limit = int(change.value)
    if limit <= int(change.previous_value):
        raise ValueError(
            f"model.max_tools_per_request only raises a model's tool cap: {model.label} takes "
            f"{change.previous_value} now, so ask for more than that."
        )
    try:
        return _test_request(agent, profile, limit)
    except Exception as exc:  # noqa: BLE001 - becomes the refusal the agent reports
        error = safe_model_error(exc)
        logger.warning(
            "A tool cap raise was not staged because its test request failed. "
            "event=model_tool_limit_test_failed agent_id=%s provider=%s model=%s limit=%d error=%s",
            getattr(agent, "id", None),
            model.provider,
            model.model_id,
            limit,
            error,
        )
        raise ValueError(f"Nothing was staged: {error} Tell the person; a higher cap for {model.label} cannot apply.") from exc


def describe_raise(change: Any) -> tuple[str, list[str]]:
    """The approval card's headline and effect lines for one raise (``RuntimeSettingsService.describe``)."""

    model = model_from_path(change.actual_path)
    headline = summary_line(
        f"Raise the tool cap of {model.label} from {change.previous_value} to {change.value} tools per request "
        "for every agent that uses it. Takes effect now."
    )
    asked_for = f" (asked for {change.target_agent_type})" if change.target_agent_type else ""
    effect = [
        f"max_tools_per_request for {model.label}{asked_for} changes from {change.previous_value} to {change.value}.",
        f"Every agent that uses {model.label} may then carry up to {change.value} tools in one request, "
        "in everyone's work.",
        f"It applies only after the model accepts one test request with {change.value} tools.",
    ]
    return headline, effect


class ModelToolLimitApplier:
    """Apply one approved ``model.max_tools_per_request`` plan as a tested, verified transaction (module docstring)."""

    def __init__(self, agent: Any) -> None:
        self.agent = agent

    def apply(
        self, config: Any, source_path: Path, changes: Sequence[Any], *, snapshot: Any, change_id: str
    ) -> dict[str, Any]:
        [change] = changes
        model = model_from_path(change.actual_path)
        limit = int(change.value)
        self._check_unchanged(source_path, snapshot)
        try:
            current, profile = target_model(self.agent, config, source_path, change.target_agent_type)
        except ValueError as exc:
            return self._failure(change_id, model, "preflight", str(exc))
        if current != model:
            return self._failure(
                change_id,
                model,
                "preflight",
                f"{change.target_agent_type or 'The agent'} uses another model than the one this plan is for "
                f"({current.label}); stage the change again.",
            )
        preflight, failure = self._preflight(profile, model, limit, change_id)
        if failure is not None:
            return failure
        # The test request may take long enough for another writer to edit the file.
        self._check_unchanged(source_path, snapshot)
        previous_existed = snapshot.existed
        previous_bytes = source_path.read_bytes() if previous_existed else b""
        payload = self._with_limit(_read_mapping(source_path), model, limit, source_path)
        backup_path = self._write(source_path, payload, previous_bytes, previous_existed, model, limit)
        use_saved_caps(self.agent, source_path)
        logger.warning(
            "A model's tool cap was raised after it was approved and a test request passed. "
            "event=model_tool_limit_changed change_id=%s agent_id=%s provider=%s model=%s limit=%d",
            change_id,
            getattr(self.agent, "id", None),
            model.provider,
            model.model_id,
            limit,
        )
        return {
            "persistence_target": PERSISTENCE_TARGET,
            "saved_path": str(source_path),
            "backup_path": str(backup_path),
            "previous_existed": previous_existed,
            "previous_sha256": sha256_bytes(previous_bytes),
            "sha256": sha256_bytes(source_path.read_bytes()),
            "preflight": preflight,
            "persistence_verification": {"status": "passed", "validated_after_write": True},
            "live_applied": [model.actual_path],
            "model_tool_limit": {"provider": model.provider, "model_id": model.model_id, MODEL_TOOL_LIMIT_FIELD: limit},
        }

    @staticmethod
    def _check_unchanged(source_path: Path, snapshot: Any) -> None:
        exists = source_path.exists()
        content = source_path.read_bytes() if exists else b""
        if exists != snapshot.existed or sha256_bytes(content) != snapshot.sha256:
            raise PermissionError(f"Runtime settings source changed after planning: {source_path}. Stage a fresh plan.")

    @staticmethod
    def _with_limit(payload: dict[str, Any], model: ModelRef, limit: int, source_path: Path) -> dict[str, Any]:
        capabilities = payload.setdefault(MODEL_CAPABILITIES_KEY, {})
        if not isinstance(capabilities, dict):
            raise ValueError(f"{MODEL_CAPABILITIES_KEY} in {source_path} must be a mapping.")
        provider_map = capabilities.setdefault(model.provider, {})
        if not isinstance(provider_map, dict):
            raise ValueError(f"{MODEL_CAPABILITIES_KEY}.{model.provider} in {source_path} must be a mapping.")
        entry = provider_map.setdefault(model.model_id, {})
        if not isinstance(entry, dict):
            raise ValueError(f"{model.actual_path.rsplit('.', 1)[0]} in {source_path} must be a mapping.")
        entry[MODEL_TOOL_LIMIT_FIELD] = limit
        return payload

    def _preflight(
        self, profile: Any, model: ModelRef, limit: int, change_id: str
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """One request with ``limit`` tools to the target model (module docstring, apply step 2)."""

        from custom_agents.runtime_settings_llm import safe_model_error

        try:
            return _test_request(self.agent, profile, limit), None
        except Exception as exc:  # noqa: BLE001 - reported to the person; nothing is written
            return {}, self._failure(change_id, model, "preflight", safe_model_error(exc))

    def _write(
        self, source_path: Path, payload: dict[str, Any], previous_bytes: bytes, previous_existed: bool, model: ModelRef, limit: int
    ) -> Path:
        from config_loader import lookup_configured_model_tool_limit, validate_llm_override_payload

        validation = validate_llm_override_payload(payload)
        if not validation.get("valid"):
            raise ValueError("LLM override file validation failed before write: " + "; ".join(validation.get("errors") or []))
        backup_path = source_path.with_name(
            f"{source_path.name}.bak.runtime-settings.{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}"
        )
        atomic_write_bytes(backup_path, previous_bytes)
        try:
            atomic_write_bytes(source_path, yaml.safe_dump(payload, sort_keys=False).encode("utf-8"))
            written = _read_mapping(source_path).get(MODEL_CAPABILITIES_KEY)
            if lookup_configured_model_tool_limit(written, provider=model.provider, model_id=model.model_id) != limit:
                raise RuntimeError(f"{source_path} did not keep the new tool cap after writing; the previous file is back.")
        except Exception:
            if previous_existed:
                atomic_write_bytes(source_path, previous_bytes)
            else:
                source_path.unlink(missing_ok=True)
            raise
        prune_config_backups(source_path, event_logger=logger, preserve_paths=(backup_path,))
        return backup_path

    def _failure(self, change_id: str, model: ModelRef, phase: str, error: str) -> dict[str, Any]:
        logger.warning(
            "A model's tool cap was not raised. event=model_tool_limit_change_failed change_id=%s agent_id=%s "
            "provider=%s model=%s phase=%s error=%s",
            change_id,
            getattr(self.agent, "id", None),
            model.provider,
            model.model_id,
            phase,
            error,
        )
        return {
            "error": error,
            "phase": phase,
            "candidate": {"provider": model.provider, "model_id": model.model_id},
            "persisted_selection_unchanged": True,
        }


__all__ = [
    "ModelRef",
    "ModelToolLimitApplier",
    "PERSISTENCE_TARGET",
    "check_and_test",
    "describe_raise",
    "effective_tool_limit",
    "model_from_path",
    "target_model",
    "use_saved_caps",
]
