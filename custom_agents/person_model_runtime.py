"""A person's own model for every agent's work for them (owner's decision 2026-10-02).

A person may save their own model on ``/auth model me``
(``PersonSettingsV1.own_model``, ``access_control.person_settings``). Every
agent's work for that person then uses it, including agent types an
administrator pinned to a model; work for nobody keeps the configured model.
Embeddings and voice keep the team's.

How a turn uses it, without touching the agent's own model settings:

- **The agent's model fields stay the team's.** Failover, the runtime
  settings apply, the reasoning-summary repair and the status views all read
  ``model_provider``, ``model_id`` and the other fields, so writing a
  person's model there would make them act on it as if it were the team's.
  The person's model lives in a per-agent ``PersonModelState`` instead.
- **Choosing it.** ``select_turn_model`` runs at the start of a turn, once the
  turn's task is known and before the context is assembled, so the prompt
  budget uses the person's context window. Who the task works for comes only
  from its verified identity (``access_control.task_identity``), never from
  message fields.
- **Using it.** The Strands agent's ``model`` is set to the person's model,
  and the turn settings (``TurnRuntimeSettings``) show its provider, model,
  address and image support (``turn_settings_with_person_model``), with no
  reasoning effort or summary, which the person did not choose.
- **Safety net.** ``PersonModelHook`` runs before every model call and sets
  the model for the agent's active task again. A resumed interrupted task, a
  fresh Strands agent made for a turn, and a rebuild after failover or a live
  settings change all go through it, and work for nobody always gets the
  team's model back.
- **Ending it.** ``clear_turn_model`` runs when the turn's execution context
  is cleared and puts the team's model back.
- **Failover** never acts on a person's model: ``person_model_active`` makes
  ``model_failover_runtime`` skip, so a person's provider running out of
  credit does not switch the team's agents to another model.
- **LLM turn summaries and compaction** (opt-in) use the person's model for
  their conversations (``summary_model_for``), so their conversation never goes
  to the team's model.

A person's model is cached per agent by the saved profile (re-read at most
every ``SETTINGS_RECHECK_SECONDS``), so saving a new one takes effect on the
next turn without a restart. A profile that cannot be built is logged and the
turn stops with a message for the person rather than quietly using the
team's model.
"""

from __future__ import annotations

import contextvars
import dataclasses
import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Iterator, TypeVar

from strands.hooks import BeforeModelCallEvent, HookProvider, HookRegistry

from access_control.person_settings import PersonSettings
from access_control.task_identity import resolve_task_identity
from custom_agents.model_keys import ModelKeyError
from custom_messages.contracts.person_settings_v1 import PersonModelProfileV1
from model_providers.endpoints import EndpointKind, display_host
from storage.access_repository import AccessRepository
from storage.config import StorageSettings

logger = logging.getLogger(__name__)

SETTINGS_RECHECK_SECONDS = 30.0
# Before the context-window check (order 0) and after tool sync (-100).
PERSON_MODEL_HOOK_ORDER = -50
_MAX_TASKS_REMEMBERED = 256
# The model factory's provider name for each kind of endpoint a person may choose.
_FACTORY_PROVIDERS = {
    EndpointKind.OPENAI_COMPATIBLE: "litellm",
    EndpointKind.OPENAI: "openai",
    EndpointKind.ANTHROPIC: "anthropic",
    EndpointKind.GEMINI: "gemini",
    EndpointKind.XAI: "xai",
}
_UNSET = object()
_Settings = TypeVar("_Settings")
_SummaryAgent = TypeVar("_SummaryAgent")


class PersonModelUnavailable(ModelKeyError):
    """The person's own model could not be built; ``str(error)`` is written for the person.

    A ``ModelKeyError`` so the turn ends the same way as a refused key: the
    person is told, and administrators are not (it is the person's model).
    """

    persons_own_matter = True


@dataclass(frozen=True)
class PersonModelBundle:
    """Everything a turn swaps together for one person's model."""

    person_id: str
    profile: PersonModelProfileV1
    provider: str
    model: Any
    context_window_tokens: int

    @property
    def describe(self) -> str:
        return f"{self.profile.model_id} at {display_host(self.profile.address)}"


@dataclass
class PersonModelState:
    """One agent's person models: the turn's choice and the built models."""

    active: PersonModelBundle | None = None
    bundles: dict[tuple[str, str], PersonModelBundle] = field(default_factory=dict)
    person_by_task: dict[str, str | None] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)


def factory_provider(kind: EndpointKind) -> str:
    return _FACTORY_PROVIDERS[kind]


# -- Reading people's saved models ------------------------------------------------------


@lru_cache(maxsize=8)
def _access_store(storage: StorageSettings) -> AccessRepository:
    return AccessRepository(storage)


_PROFILE_CACHE: dict[tuple[str, str], tuple[float, PersonModelProfileV1 | None]] = {}
_PROFILE_LOCK = threading.Lock()


def own_model(agent: Any, person_id: str) -> PersonModelProfileV1 | None:
    """The person's saved model, re-read at most every ``SETTINGS_RECHECK_SECONDS``."""

    team_id = str(getattr(agent, "team_id", "") or "")
    cache_key = (team_id, person_id)
    now = time.monotonic()
    with _PROFILE_LOCK:
        cached = _PROFILE_CACHE.get(cache_key)
    if cached is not None and now - cached[0] < SETTINGS_RECHECK_SECONDS:
        return cached[1]
    storage = getattr(getattr(agent, "task_manager", None), "storage_settings", None)
    if storage is None or not team_id:
        return None
    try:
        profile = PersonSettings(_access_store(storage), team_id).own_model(person_id)
    except Exception:  # noqa: BLE001 - the broker still refuses the team's key where it must
        logger.exception(
            "Could not read a person's own model; keeping the last one known. event=person_model_read_failed "
            "person_id=%s",
            person_id,
        )
        return cached[1] if cached is not None else None
    with _PROFILE_LOCK:
        _PROFILE_CACHE[cache_key] = (now, profile)
    return profile


def forget_cached_profiles() -> None:
    """Drop the cache of saved models (tests)."""

    with _PROFILE_LOCK:
        _PROFILE_CACHE.clear()


# -- Whose task ---------------------------------------------------------------------------


def person_for_meta(agent: Any, meta: Any) -> str | None:
    """The person a task's verified identity names; None for work for nobody or an unverifiable task."""

    gateway_context = getattr(meta, "gateway_context", None)
    signed = getattr(gateway_context, "task_context", None)
    run = getattr(gateway_context, "schedule_run", None)
    if signed is None and run is None:
        return None
    try:
        from custom_agents.access_runtime import runtime_access

        access = runtime_access(agent)
        identity = resolve_task_identity(
            verifier=access.verifier,
            team_id=access.team_id,
            task_context=signed.model_dump(mode="json") if signed is not None else None,
            schedule_run=run.model_dump(mode="json") if run is not None else None,
            schedule_runs=access.schedule_runs,
        )
    except Exception as exc:  # noqa: BLE001 - unverifiable: the team's model, as for work for nobody
        logger.info(
            "A task's identity did not verify, so it uses the configured model. event=person_model_identity_unverified "
            "agent_id=%s reason=%s",
            getattr(agent, "id", None),
            exc,
        )
        return None
    return identity.person_id


def person_for_task(agent: Any, task_id: str | None) -> str | None:
    if not task_id:
        return None
    state = _state(agent)
    with state.lock:
        if task_id in state.person_by_task:
            return state.person_by_task[task_id]
    task_manager = getattr(agent, "task_manager", None)
    task_info = task_manager.get_task_info(task_id) if task_manager is not None else None
    person = person_for_meta(agent, getattr(task_info, "meta", None)) if task_info is not None else None
    with state.lock:
        if len(state.person_by_task) >= _MAX_TASKS_REMEMBERED:
            state.person_by_task.pop(next(iter(state.person_by_task)))
        state.person_by_task[task_id] = person
    return person


# -- Building and choosing ------------------------------------------------------------------


def _state(agent: Any) -> PersonModelState:
    state = getattr(agent, "_person_models", None)
    if state is None:
        state = PersonModelState()
        setattr(agent, "_person_models", state)
    return state


def bundle_for_person(agent: Any, person_id: str | None) -> PersonModelBundle | None:
    """The person's model for this agent, built once per saved profile; None when they have none."""

    if not person_id:
        return None
    profile = own_model(agent, person_id)
    if profile is None:
        return None
    state = _state(agent)
    cache_key = (person_id, profile.tested_at_utc.isoformat())
    with state.lock:
        bundle = state.bundles.get(cache_key)
    if bundle is not None:
        return bundle
    bundle = _build_bundle(agent, person_id, profile)
    with state.lock:
        # A newer profile replaces the person's older models.
        for stale in [key for key in state.bundles if key[0] == person_id]:
            state.bundles.pop(stale, None)
        state.bundles[cache_key] = bundle
    logger.info(
        "Built a person's own model. event=person_model_built agent_id=%s person_id=%s provider=%s model_id=%s "
        "address=%s",
        getattr(agent, "id", None),
        person_id,
        bundle.provider,
        profile.model_id,
        profile.address,
    )
    return bundle


def _build_bundle(agent: Any, person_id: str, profile: PersonModelProfileV1) -> PersonModelBundle:
    from custom_agents.runtime_paths import infer_instance_root
    from custom_agents.strands_model_factory import StrandsModelFactoryConfig, build_strands_model

    provider = factory_provider(profile.kind)
    window = profile.context_window_tokens or int(agent._resolved_context_window_tokens())
    try:
        model = build_strands_model(
            StrandsModelFactoryConfig(
                provider=provider,
                model_id=profile.model_id,
                max_output_tokens=getattr(agent, "max_output_tokens", None),
                context_window_tokens=window,
                litellm_api_mode=profile.api_mode,
                request_timeout_seconds=getattr(agent, "request_timeout_seconds", None),
                stream_idle_timeout_seconds=agent.stream_idle_timeout_seconds,
                model_call_timeout_seconds=agent.model_call_timeout_seconds,
                reasoning_trace_retention_messages=getattr(agent, "reasoning_trace_retention_messages", None),
                litellm_api_base=profile.base_url if profile.kind is EndpointKind.OPENAI_COMPATIBLE else None,
                instance_root=infer_instance_root(agent),
            )
        )
    except Exception as exc:
        logger.exception(
            "A person's own model could not be built. event=person_model_build_failed person_id=%s provider=%s "
            "model_id=%s",
            person_id,
            provider,
            profile.model_id,
        )
        raise PersonModelUnavailable(
            f"Your own model ({profile.model_id} at {display_host(profile.address)}) could not be started, so this "
            "request did not run. Check it, or remove it to use the team's model if you may, with /auth model me."
        ) from exc
    return PersonModelBundle(
        person_id=person_id, profile=profile, provider=provider, model=model, context_window_tokens=window
    )


def select_turn_model(agent: Any, task_id: str | None) -> PersonModelBundle | None:
    """Choose the model for the turn of ``task_id`` (module docstring) and put it in place."""

    bundle = bundle_for_person(agent, person_for_task(agent, task_id))
    _state(agent).active = bundle
    apply_model(agent)
    if bundle is not None:
        logger.info(
            "This turn uses the person's own model. event=person_model_selected agent_id=%s task_id=%s "
            "person_id=%s model=%s",
            getattr(agent, "id", None),
            task_id,
            bundle.person_id,
            bundle.describe,
        )
    return bundle


def clear_turn_model(agent: Any) -> None:
    """Put the team's model back when the turn ends."""

    state = _state(agent)
    if state.active is None:
        return
    state.active = None
    apply_model(agent)


def apply_model(agent: Any, strands_agent: Any = _UNSET) -> None:
    """Set the Strands agent's model and the context window for the active choice."""

    bundle = _state(agent).active
    target = strands_agent if strands_agent is not _UNSET else getattr(agent, "_strands_agent", None)
    model = bundle.model if bundle is not None else getattr(agent, "_strands_model", None)
    if target is not None and model is not None and getattr(target, "model", None) is not model:
        target.model = model
    engine = getattr(agent, "_context_engine", None)
    if engine is not None and hasattr(engine, "context_window_tokens"):
        engine.context_window_tokens = (
            bundle.context_window_tokens if bundle is not None else agent._resolved_context_window_tokens()
        )


def active_bundle(agent: Any) -> PersonModelBundle | None:
    return _state(agent).active


def person_model_active(agent: Any) -> bool:
    """Whether the agent's current turn runs on a person's own model (failover then stays out)."""

    return _state(agent).active is not None


def turn_settings_with_person_model(agent: Any, settings: _Settings) -> _Settings:
    """The turn settings, showing the person's model while it is active (module docstring)."""

    bundle = _state(agent).active
    if bundle is None:
        return settings
    profile = bundle.profile
    return dataclasses.replace(
        settings,  # type: ignore[type-var]  # TurnRuntimeSettings, a dataclass
        model_provider=bundle.provider,
        model_id=profile.model_id,
        litellm_api_base=profile.base_url if profile.kind is EndpointKind.OPENAI_COMPATIBLE else None,
        litellm_api_mode=profile.api_mode,
        reasoning_effort=None,
        reasoning_summary=None,
        supports_image_input=profile.supports_images,
    )


def model_supports(agent: Any, capability: str) -> bool | None:
    """Whether the model in use takes ``image``, ``audio`` or ``file`` input (the person's model while active)."""

    bundle = _state(agent).active
    if bundle is None:
        return getattr(agent, f"supports_{capability}_input", None)
    return {"image": bundle.profile.supports_images, "file": bundle.profile.supports_files}.get(capability, False)


def summary_model_for(agent: Any, meta: Any) -> Any | None:
    """The person's model for summarizing a conversation of theirs (``meta`` is the task's); None keeps the team's."""

    active = _state(agent).active
    person = person_for_meta(agent, meta)
    if active is not None and active.person_id == person:
        return active.model
    try:
        bundle = bundle_for_person(agent, person)
    except PersonModelUnavailable:
        return None
    return bundle.model if bundle is not None else None


# The person's model for summaries made outside their turn (conversation finalization).
_SUMMARY_MODEL: contextvars.ContextVar[Any] = contextvars.ContextVar("person_summary_model", default=None)


@contextmanager
def summaries_with_person_model(meta: Any) -> Iterator[None]:
    """While finalizing a conversation turn, summaries use the model of the person the task works for."""

    from custom_tools.runtime_context import get_agent_ref

    agent = get_agent_ref()
    model = summary_model_for(agent, meta) if agent is not None and meta is not None else None
    token = _SUMMARY_MODEL.set(model)
    try:
        yield
    finally:
        _SUMMARY_MODEL.reset(token)


def current_summary_model() -> Any | None:
    """The person's model for a summary now (finalization, or a turn on their model); None keeps the summarizer's."""

    model = _SUMMARY_MODEL.get()
    if model is not None:
        return model
    from custom_tools.runtime_context import get_agent_ref

    agent = get_agent_ref()
    bundle = _state(agent).active if agent is not None and getattr(agent, "_person_models", None) else None
    return bundle.model if bundle is not None else None


class SummaryAgents:
    """A summarizer's Strands agent for the model in use: its own, or a person's (one per model, never shared)."""

    def __init__(self, system_prompt: str) -> None:
        self._system_prompt = system_prompt
        self._by_model: dict[int, Any] = {}
        self._lock = threading.Lock()

    def agent_for(self, default_agent: _SummaryAgent) -> _SummaryAgent:
        model = current_summary_model()
        if model is None:
            return default_agent
        with self._lock:
            agent = self._by_model.get(id(model))
            if agent is None:
                from strands import Agent
                from strands.agent.conversation_manager.null_conversation_manager import NullConversationManager

                agent = Agent(
                    model=model, tools=[], system_prompt=self._system_prompt, conversation_manager=NullConversationManager()
                )
                self._by_model = {id(model): agent}  # keep only the latest person's agent
            return agent  # type: ignore[return-value]  # the same Strands agent type as the default


class PersonModelHook(HookProvider):
    """Before every model call, the model of the agent's active task (module docstring)."""

    def __init__(self, agent: Any) -> None:
        self._agent = agent

    def register_hooks(self, registry: HookRegistry, **_kwargs: Any) -> None:
        registry.add_callback(BeforeModelCallEvent, self._before_model_call, order=PERSON_MODEL_HOOK_ORDER)

    def _before_model_call(self, event: BeforeModelCallEvent) -> None:
        from custom_tools.runtime_context import active_task_context

        state = _state(self._agent)
        task_id, _meta = active_task_context(self._agent)
        person = person_for_task(self._agent, task_id)
        if state.active is None or state.active.person_id != person:
            state.active = bundle_for_person(self._agent, person)
        apply_model(self._agent, event.agent)


__all__ = [
    "PERSON_MODEL_HOOK_ORDER",
    "PersonModelBundle",
    "PersonModelHook",
    "PersonModelState",
    "PersonModelUnavailable",
    "SummaryAgents",
    "current_summary_model",
    "summaries_with_person_model",
    "active_bundle",
    "apply_model",
    "bundle_for_person",
    "clear_turn_model",
    "factory_provider",
    "forget_cached_profiles",
    "model_supports",
    "own_model",
    "person_for_meta",
    "person_for_task",
    "person_model_active",
    "select_turn_model",
    "summary_model_for",
    "turn_settings_with_person_model",
]
