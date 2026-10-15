"""The team's OpenAI-compatible endpoint takes effect live, at each agent's next turn.

``llm.litellm_api_base`` used to apply only after a manager restart, because
each process read it once at start (through the launcher's
``LITELLM_API_BASE``). A manager restart restarts only TeamLead's process, so
startup agents such as MemReviewAgent kept calling the old endpoint until the
whole container restarted (owner's report 2026-10-02, who also asked why a
switch needs a restart at all: it does not).

Now every agent checks the saved instance configuration at the start of each
turn (``refresh_team_endpoint``, called from
``model_failover_runtime.refresh_runtime_llm_selection``, the step that
already re-reads ``LLM_OVERRIDES.yaml`` every turn):

- The file is read again only when it changed (size and modification time),
  so a turn normally costs one ``stat``.
- The agent type's own ``agent_types.<type>.llm.litellm_api_base`` wins over
  the team's ``llm.litellm_api_base``, as at start.
- When the endpoint differs from the one in use, the agent rebuilds its model,
  compaction and summary components (``_rebuild_runtime_llm_components``) and
  restores the old ones if that fails, so a bad build never leaves an agent
  without a model. It happens only between turns: a turn that is running
  finishes on the endpoint it started with.
- ``LITELLM_API_BASE`` set by the operator (``team.env`` or the container
  environment, as listed by the launcher) still wins, as it did before; the
  switch is refused while it is set (``endpoint_switch``), and an agent never
  replaces it. A value the launcher derived from the saved file is updated in
  the process environment, so agents this process starts later get the new
  endpoint too.

Agents switch one by one at their next turn, so for a short while the team
may use both endpoints. The endpoint is not part of ``RuntimeLLMProfile`` or
``LLM_OVERRIDES.yaml`` on purpose: that file is also written by model-choice
plans and repairs that need no administrator review, and the endpoint must
keep its administrator approval.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any

from custom_agents.model_runtime_profile import RUNTIME_LLM_REFERENCES

logger = logging.getLogger(__name__)

ENDPOINT_ENV_VAR = "LITELLM_API_BASE"
_CACHE: dict[str, tuple[tuple[int, int], dict[str, str], bool]] = {}
_CACHE_LOCK = threading.Lock()


def refresh_team_endpoint(agent: Any) -> bool:
    """Apply a changed saved endpoint to ``agent`` (module docstring); True when it switched."""

    saved = saved_endpoint(agent)
    if saved is None:
        return False
    current = str(getattr(agent, "litellm_api_base", "") or "")
    if saved.rstrip("/") == current.rstrip("/"):
        return False
    previous_references = {name: getattr(agent, name, None) for name in RUNTIME_LLM_REFERENCES}
    agent.litellm_api_base = saved
    try:
        agent._rebuild_runtime_llm_components()
    except Exception:
        agent.litellm_api_base = current
        for name, value in previous_references.items():
            setattr(agent, name, value)
        logger.exception(
            "Could not switch to the saved model endpoint; the agent keeps the old one. "
            "event=model_endpoint_switch_failed agent_id=%s named_agent_type=%s old=%s new=%s",
            getattr(agent, "id", None),
            getattr(agent, "named_agent_type", None),
            current,
            saved,
        )
        return False
    _update_derived_environment(saved)
    logger.info(
        "Switched to the saved model endpoint at the start of a turn. event=model_endpoint_switched_live "
        "agent_id=%s named_agent_type=%s old=%s new=%s",
        getattr(agent, "id", None),
        getattr(agent, "named_agent_type", None),
        current,
        saved,
    )
    return True


def saved_endpoint(agent: Any) -> str | None:
    """The endpoint the saved configuration gives this agent's type; None when unknown or overridden by the operator."""

    path_text = str(getattr(agent, "instance_config_path", "") or "")
    if not path_text:
        return None
    path = Path(path_text)
    try:
        info = path.stat()
    except OSError:
        return None
    fingerprint = (info.st_mtime_ns, info.st_size)
    with _CACHE_LOCK:
        cached = _CACHE.get(path_text)
    if cached is None or cached[0] != fingerprint:
        loaded = _load(path)
        if loaded is None:
            return None
        cached = (fingerprint, *loaded)
        with _CACHE_LOCK:
            _CACHE[path_text] = cached
    _fingerprint, endpoints, overridden = cached
    if overridden:
        return None
    agent_type = str(getattr(agent, "named_agent_type", "") or "")
    return endpoints.get(agent_type) or endpoints.get("") or None


def forget_cached_configuration() -> None:
    """Drop the cache of saved files (tests)."""

    with _CACHE_LOCK:
        _CACHE.clear()


def _load(path: Path) -> tuple[dict[str, str], bool] | None:
    """The team's endpoint (key ``""``), each agent type's own, and whether the operator overrides it."""

    from assistant_gateway.config import GatewayInstanceConfig
    from custom_agents.endpoint_switch import operator_overrides_endpoint

    try:
        config = GatewayInstanceConfig.load_saved(path)
    except Exception as exc:  # noqa: BLE001 - a half-written or invalid file waits for the next turn
        logger.warning(
            "Could not read the saved configuration for the model endpoint; keeping the current one. "
            "event=model_endpoint_config_unreadable path=%s error=%s",
            path,
            exc,
        )
        return None
    endpoints = {"": str(config.llm.litellm_api_base or "")}
    for agent_type, settings in (config.agent_types or {}).items():
        override = getattr(getattr(settings, "llm", None), "litellm_api_base", None)
        if override:
            endpoints[agent_type] = str(override)
    return endpoints, operator_overrides_endpoint(config)


def _update_derived_environment(endpoint: str) -> None:
    """Agents this process starts later read ``LITELLM_API_BASE``; the launcher derived it from the saved file."""

    if os.environ.get(ENDPOINT_ENV_VAR) is not None:
        os.environ[ENDPOINT_ENV_VAR] = endpoint


__all__ = ["forget_cached_configuration", "refresh_team_endpoint", "saved_endpoint"]
