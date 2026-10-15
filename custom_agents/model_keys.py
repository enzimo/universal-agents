"""The model key for each endpoint a process calls: an environment override, or the broker's stored key.

Model keys are stored per endpoint address in the broker's credential store
(``secure_tool_broker.model_keys``); administrators save them with ``/auth
model``. This module is the only place the agent runtime gets a model key:
the model factory, compaction, summaries, embeddings and the model capability
check all ask it.

How a key is chosen for an endpoint (``model_providers.endpoints``):

1. An environment variable from ``endpoint.env_vars`` (``LITELLM_API_KEY``
   and its ``_FILE`` form, for example) wins when set: operators keep that
   override (owner's decision 2026-09-30). It is never silent: the process
   logs it once and reports it to the broker by name, and ``/auth model``
   and the Status page show it.
2. Otherwise the broker's stored key for the address. A stored key is
   re-checked at most every ``RECHECK_SECONDS``, at once after a provider
   rejected it, and kept (with a warning) when the broker does not answer, so
   a broker restart does not stop running agents. With nothing cached, a
   lookup waits up to ``BROKER_WAIT_SECONDS`` for the broker, which starts
   after the manager.

For a task that works for a person, the broker is asked first, before any
environment variable: it answers with the person's own key when they saved
one, and refuses the team's key when an administrator decided their work
uses their own model (``access_control.person_settings``). That refusal
(``ModelKeyNotAllowedError``) also covers an environment override, which is
the team's key too. Embeddings are the team's service and keep the team's
key (``purpose``). With no broker configured there are no per-person
settings, and the environment and team keys apply as before.

A key is only ever sent to the address it belongs to: ``bind_model_keys``
looks up the key of the endpoint a model was built for, before every call.
When the provider rejects it, the call ends with ``ModelKeyRejectedError``,
whose message tells the person what happened and who fixes it, the broker
hears about it, and the next call asks the broker again, so a new key saved
with ``/auth model`` works without a restart.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import json
import logging
import os
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, AsyncGenerator, Awaitable, Callable, Iterator, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from custom_agents.model_key_errors import ModelKeyError
from custom_agents.provider_error_runtime import is_provider_key_rejected_error, provider_error_status_code
from secure_tool_broker.defaults import MODEL_KEY_ENDPOINTS_ROUTE, MODEL_KEY_LOOKUP_ROUTE, MODEL_KEY_REPORT_ROUTE
from custom_messages.contracts.model_keys_v1 import (
    ConnectionPurposeV1,
    KeyReportEvent,
    KeySource,
    ModelKeyLookupV1,
    ModelKeyReplyV1,
    ModelKeyReportV1,
    SavedEndpointsV1,
    StoredKeyState,
)
from env_secrets import get_env_with_file_fallback
from secure_tools.chat_commands import MODEL_KEY_COMMAND, OWN_MODEL_COMMAND, SETUP_COMMAND
from model_providers.endpoints import ModelEndpoint, embedding_endpoint

logger = logging.getLogger(__name__)

RECHECK_SECONDS = 60.0
REPORT_REPEAT_SECONDS = 600.0
BROKER_WAIT_SECONDS = 30.0
_BROKER_RETRY_SECONDS = (1.0, 2.0, 4.0, 8.0)
_BROKER_TIMEOUT_SECONDS = 10.0
_STALE_WARNING_SECONDS = 60.0
# Sent where a client library insists on some key although the endpoint was
# saved as needing none (an OpenAI client refuses an empty key).
NO_KEY_PLACEHOLDER = "no-key-needed"
_BROKER_URL_ENV = "SECURE_TOOL_BROKER_INTERNAL_BASE_URL"
_BROKER_TOKEN_ENV = "SECURE_TOOL_BROKER_TOKEN"


class ModelKeyMissingError(ModelKeyError):
    def __init__(self, endpoint: ModelEndpoint) -> None:
        self.address = endpoint.address
        names = " or ".join(endpoint.env_vars)
        super().__init__(
            f"There is no key for the model provider at {endpoint.host}, so this request did not run. "
            f"An administrator can save one with {MODEL_KEY_COMMAND} (or set {names})."
        )


class ModelKeyUnavailableError(ModelKeyError):
    def __init__(self, endpoint: ModelEndpoint, reason: str) -> None:
        self.address = endpoint.address
        super().__init__(
            f"Agents could not get the key for the model provider at {endpoint.host} from the secure tool "
            f"broker ({reason}), so this request did not run. An administrator checks the broker and its "
            "credential store, including Sealbox tenant token and key files. If existing tenant files "
            "are missing or damaged, restore them from a backup; then send your request again."
        )


class ModelKeyNotAllowedError(ModelKeyError):
    """The task's person may not use the team's model keys and saved none of their own for this address."""

    persons_own_matter = True

    def __init__(self, endpoint: ModelEndpoint) -> None:
        self.address = endpoint.address
        super().__init__(own_model_needed_message())


def own_model_needed_message() -> str:
    """What a person hears when their work may not use the team's model keys (owner's wording 2026-10-02).

    It names ``/setup`` (``assistant_gateway.personal_setup``), which lists
    everything the person still sets up for their own work.
    """

    return (
        f"You haven't set up your own model yet. Use {OWN_MODEL_COMMAND}. An administrator decided that "
        "agents use your own model provider for your work, so this request did not run. "
        f"Send {SETUP_COMMAND} to see everything you still set up for your own work."
    )


class ModelKeyRejectedError(ModelKeyError):
    """The provider refused the key: who fixes it depends on where the key came from."""

    def __init__(self, endpoint: ModelEndpoint, resolved: "ResolvedKey", status_code: int | None) -> None:
        self.address = endpoint.address
        self.host = endpoint.host
        self.source = resolved.source
        self.env_var = resolved.env_var
        self.status_code = status_code
        super().__init__(rejection_message(endpoint.host, resolved.source, resolved.env_var))


def rejection_message(host: str, source: KeySource, env_var: str | None) -> str:
    """What the person is told when the provider at ``host`` refused the key, and who fixes it."""

    if source is KeySource.ENVIRONMENT:
        return (
            f"The model provider at {host} rejected the key in the {env_var} environment variable, so this "
            f"request did not run. An administrator updates {env_var} in team.env (or its secret file) and "
            "restarts the container; then send your request again."
        )
    if source is KeySource.PERSONAL:
        return (
            f"The model provider at {host} rejected your own key, so this request did not run. Replace it, or "
            f"remove it to use the team's key, with {OWN_MODEL_COMMAND}; then send your request again."
        )
    if source is KeySource.NO_KEY_NEEDED:
        return (
            f"The model provider at {host} asked for a key, but the team saved it as needing none, so this "
            f"request did not run. An administrator can save its key with {MODEL_KEY_COMMAND}; then send your "
            "request again."
        )
    return (
        f"The model provider at {host} rejected the team's key, so this request did not run. An administrator "
        f"can save a new key with {MODEL_KEY_COMMAND}; then send your request again."
    )


@dataclass(frozen=True)
class ResolvedKey:
    address: str
    value: str = field(repr=False)
    source: KeySource
    env_var: str | None = None

    @property
    def sendable(self) -> str:
        """The value to hand a client library (a placeholder when no key is needed)."""

        return self.value or NO_KEY_PLACEHOLDER


class BrokerUnreachable(OSError):
    """The broker did not answer (not running yet, or down)."""


class BrokerNotConfigured(RuntimeError):
    """This process has no broker address or token, so only environment variables give keys."""


class ModelKeyBroker(Protocol):
    def lookup(
        self,
        address: str,
        context: Mapping[str, Any] | None = None,
        *,
        purpose: ConnectionPurposeV1 = ConnectionPurposeV1.CHAT,
    ) -> ModelKeyReplyV1: ...

    def report(self, report: ModelKeyReportV1) -> None: ...


class HttpModelKeyBroker:
    """The broker's model key routes, called with this process's own caller token.

    The address and token are read from the environment at each call: the
    manager sets the broker's address while it starts (``strands_team``),
    possibly after the first model was built.
    """

    def lookup(
        self,
        address: str,
        context: Mapping[str, Any] | None = None,
        *,
        purpose: ConnectionPurposeV1 = ConnectionPurposeV1.CHAT,
    ) -> ModelKeyReplyV1:
        body = ModelKeyLookupV1(address=address, context=dict(context) if context else None, purpose=purpose)
        payload = self._post(MODEL_KEY_LOOKUP_ROUTE, body.model_dump(mode="json"))
        return ModelKeyReplyV1.model_validate(payload)

    def report(self, report: ModelKeyReportV1) -> None:
        self._post(MODEL_KEY_REPORT_ROUTE, report.model_dump(mode="json"))

    def saved_endpoints(self) -> SavedEndpointsV1:
        """The team's endpoints with a saved key (addresses only, never keys)."""

        return SavedEndpointsV1.model_validate(self._post(MODEL_KEY_ENDPOINTS_ROUTE, None))

    def _post(self, route: str, body: dict[str, Any] | None) -> dict[str, Any]:
        """POST ``body`` to the route, or GET it when there is no body."""

        base_url = str(os.getenv(_BROKER_URL_ENV) or "").strip().rstrip("/")
        token = str(os.getenv(_BROKER_TOKEN_ENV) or "").strip()
        if not base_url or not token:
            raise BrokerNotConfigured(f"{_BROKER_URL_ENV} or {_BROKER_TOKEN_ENV} is not set for this process")
        request = Request(
            f"{base_url}{route}",
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            method="POST" if body is not None else "GET",
        )
        try:
            with urlopen(request, timeout=_BROKER_TIMEOUT_SECONDS) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            if exc.code >= 500:
                raise BrokerUnreachable(f"the broker answered HTTP {exc.code}: {detail}") from exc
            raise RuntimeError(f"The broker refused the model key request (HTTP {exc.code}): {detail}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise BrokerUnreachable(str(getattr(exc, "reason", exc))) from exc
        payload = json.loads(raw) if raw else {}
        return payload if isinstance(payload, dict) else {}


@dataclass
class _Cached:
    resolved: ResolvedKey
    fetched_at: float
    stale_warned_at: float = 0.0


EnvLookup = Callable[[str], "str | None"]


class ModelKeys:
    """One process's model keys: environment overrides, then the broker's stored keys (module docstring)."""

    def __init__(
        self,
        broker: ModelKeyBroker | None,
        *,
        env_lookup: EnvLookup = get_env_with_file_fallback,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._broker = broker
        self._env_lookup = env_lookup
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        # By (address, person id or "" for the team's key).
        self._cache: dict[tuple[str, str], _Cached] = {}
        self._reported: dict[tuple[str, KeySource, str | None], float] = {}
        self._env_logged: set[tuple[str, str]] = set()
        # The last key resolved for each address, from any source, so error
        # text can be cleaned of every key this process holds.
        self._held: dict[tuple[str, str], ResolvedKey] = {}

    def resolve(
        self,
        endpoint: ModelEndpoint,
        context: Mapping[str, Any] | None = None,
        *,
        purpose: ConnectionPurposeV1 = ConnectionPurposeV1.CHAT,
    ) -> ResolvedKey:
        """The key to send to ``endpoint`` now; raises ``ModelKeyError`` with a message for the person.

        ``context`` is the task's trusted broker context: with it, the broker
        answers with that person's own key when they saved one, and refuses
        the team's key when their work may not use it (module docstring).
        """

        resolved = self._person_first(endpoint, context, purpose) if _person(context) else None
        if resolved is None or resolved.source is not KeySource.PERSONAL:
            resolved = self._from_environment(endpoint) or resolved or self._from_broker(endpoint, context, purpose)
        with self._lock:
            self._held[(endpoint.address, _person(context))] = resolved
        self._report(endpoint, resolved, KeyReportEvent.IN_USE)
        return resolved

    async def resolve_async(
        self,
        endpoint: ModelEndpoint,
        context: Mapping[str, Any] | None = None,
        *,
        purpose: ConnectionPurposeV1 = ConnectionPurposeV1.CHAT,
    ) -> ResolvedKey:
        cached = self._fresh_cached((endpoint.address, _person(context)))
        if cached is not None and self._environment_value(endpoint) is None:
            return cached
        return await asyncio.to_thread(functools.partial(self.resolve, endpoint, context, purpose=purpose))

    def rejected(
        self,
        endpoint: ModelEndpoint,
        resolved: ResolvedKey,
        *,
        provider: str,
        model_id: str,
        status_code: int | None,
        context: Mapping[str, Any] | None = None,
    ) -> None:
        """The provider refused ``resolved``: forget it, so the next call asks again, and tell the broker.

        A person's own key is their matter: it is forgotten and logged, not
        reported as the team's key state.
        """

        with self._lock:
            self._cache.pop((endpoint.address, _person(context)), None)
        logger.warning(
            "A model provider rejected a key. event=model_key_rejected address=%s source=%s env_var=%s "
            "provider=%s model_id=%s status_code=%s",
            endpoint.address,
            resolved.source.value,
            resolved.env_var,
            provider,
            model_id,
            status_code,
        )
        if resolved.source is KeySource.PERSONAL:
            return
        self._send_report(
            ModelKeyReportV1(
                address=endpoint.address,
                kind=endpoint.kind,
                event=KeyReportEvent.REJECTED,
                source=resolved.source,
                env_var=resolved.env_var,
                provider=provider,
                model_id=model_id,
                status_code=status_code,
            )
        )

    def known_values(self) -> tuple[str, ...]:
        """Keys this process holds, so error text can be cleaned of them."""

        with self._lock:
            return tuple(resolved.value for resolved in self._held.values() if resolved.value)

    # -- Sources ----------------------------------------------------------------------

    def _environment_value(self, endpoint: ModelEndpoint) -> tuple[str, str] | None:
        for name in endpoint.env_vars:
            value = str(self._env_lookup(name) or "").strip()
            if value:
                return name, value
        return None

    def _from_environment(self, endpoint: ModelEndpoint) -> ResolvedKey | None:
        found = self._environment_value(endpoint)
        if found is None:
            return None
        name, value = found
        if (endpoint.address, name) not in self._env_logged:
            self._env_logged.add((endpoint.address, name))
            logger.warning(
                "An environment variable overrides the stored model key. event=model_key_environment_override "
                "address=%s env_var=%s",
                endpoint.address,
                name,
            )
        return ResolvedKey(address=endpoint.address, value=value, source=KeySource.ENVIRONMENT, env_var=name)

    def _fresh_cached(self, cache_key: tuple[str, str]) -> ResolvedKey | None:
        with self._lock:
            cached = self._cache.get(cache_key)
        if cached is not None and self._clock() - cached.fetched_at < RECHECK_SECONDS:
            return cached.resolved
        return None

    def _person_first(
        self, endpoint: ModelEndpoint, context: Mapping[str, Any] | None, purpose: ConnectionPurposeV1
    ) -> ResolvedKey | None:
        """For a person's task, the broker's answer before any environment override (module docstring).

        None when the broker has no key for the address and the person may
        use the team's keys: an environment variable may still give one.
        """

        try:
            return self._from_broker(endpoint, context, purpose)
        except ModelKeyMissingError:
            return None

    def _from_broker(
        self,
        endpoint: ModelEndpoint,
        context: Mapping[str, Any] | None,
        purpose: ConnectionPurposeV1 = ConnectionPurposeV1.CHAT,
    ) -> ResolvedKey:
        cache_key = (endpoint.address, _person(context))
        fresh = self._fresh_cached(cache_key)
        if fresh is not None:
            return fresh
        if self._broker is None:
            raise ModelKeyMissingError(endpoint)
        try:
            reply = self._lookup_with_wait(endpoint, context, purpose)
        except BrokerUnreachable as exc:
            return self._stale_or_raise(endpoint, cache_key, exc)
        except BrokerNotConfigured as exc:
            logger.warning(
                "No stored model key can be used without the broker. event=model_keys_without_broker "
                "address=%s reason=%s",
                endpoint.address,
                exc,
            )
            raise ModelKeyMissingError(endpoint) from exc
        if not reply.team_keys_allowed:
            with self._lock:
                self._cache.pop(cache_key, None)
            logger.info(
                "A person's work may not use the team's model keys; the call does not run. "
                "event=model_key_team_keys_refused address=%s person_id=%s",
                endpoint.address,
                _person(context),
            )
            raise ModelKeyNotAllowedError(endpoint)
        if reply.state is StoredKeyState.MISSING:
            with self._lock:
                self._cache.pop(cache_key, None)
            raise ModelKeyMissingError(endpoint)
        if reply.owner == "person":
            source = KeySource.PERSONAL
        elif reply.state is StoredKeyState.NO_KEY_NEEDED:
            source = KeySource.NO_KEY_NEEDED
        else:
            source = KeySource.STORED
        resolved = ResolvedKey(address=endpoint.address, value=reply.key, source=source)
        with self._lock:
            self._cache[cache_key] = _Cached(resolved=resolved, fetched_at=self._clock())
        return resolved

    def _lookup_with_wait(
        self, endpoint: ModelEndpoint, context: Mapping[str, Any] | None, purpose: ConnectionPurposeV1
    ) -> ModelKeyReplyV1:
        assert self._broker is not None
        with self._lock:
            has_cached = (endpoint.address, _person(context)) in self._cache
        delays = () if has_cached else _BROKER_RETRY_SECONDS
        waited = 0.0
        for delay in (*delays, None):
            try:
                return self._broker.lookup(endpoint.address, context, purpose=purpose)
            except BrokerUnreachable:
                if delay is None or waited + delay > BROKER_WAIT_SECONDS:
                    raise
                logger.info(
                    "Waiting for the broker to answer a model key lookup. event=model_key_broker_wait address=%s "
                    "waited_seconds=%.0f",
                    endpoint.address,
                    waited,
                )
                self._sleep(delay)
                waited += delay
        raise BrokerUnreachable("no answer")  # pragma: no cover - the loop returns or raises

    def _stale_or_raise(self, endpoint: ModelEndpoint, cache_key: tuple[str, str], exc: BrokerUnreachable) -> ResolvedKey:
        with self._lock:
            cached = self._cache.get(cache_key)
            now = self._clock()
            warn = cached is not None and now - cached.stale_warned_at >= _STALE_WARNING_SECONDS
            if warn and cached is not None:
                cached.stale_warned_at = now
        if cached is None:
            logger.error(
                "No model key: the broker did not answer and none is cached. event=model_key_broker_unreachable "
                "address=%s error=%s",
                endpoint.address,
                exc,
            )
            raise ModelKeyUnavailableError(endpoint, str(exc)) from exc
        if warn:
            logger.warning(
                "The broker did not answer; using the last model key it gave. event=model_key_stale_used "
                "address=%s error=%s",
                endpoint.address,
                exc,
            )
        return cached.resolved

    # -- Reports ----------------------------------------------------------------------

    def _report(self, endpoint: ModelEndpoint, resolved: ResolvedKey, event: KeyReportEvent) -> None:
        if resolved.source is KeySource.PERSONAL:
            return
        marker = (endpoint.address, resolved.source, resolved.env_var)
        now = self._clock()
        with self._lock:
            last = self._reported.get(marker)
            if last is not None and now - last < REPORT_REPEAT_SECONDS:
                return
            self._reported[marker] = now
        self._send_report(
            ModelKeyReportV1(
                address=endpoint.address, kind=endpoint.kind, event=event, source=resolved.source, env_var=resolved.env_var
            )
        )

    def _send_report(self, report: ModelKeyReportV1) -> None:
        if self._broker is None:
            return
        try:
            self._broker.report(report)
        except BrokerNotConfigured:
            return
        except (BrokerUnreachable, RuntimeError) as exc:
            # Reports feed /auth model and the Status page; a lost one is
            # sent again within REPORT_REPEAT_SECONDS and never stops a call.
            logger.warning(
                "Could not tell the broker which model key is in use. event=model_key_report_failed address=%s "
                "event_kind=%s error=%s",
                report.address,
                report.event.value,
                exc,
            )


def _person(context: Mapping[str, Any] | None) -> str:
    return str((context or {}).get("canonical_user_id") or "")


def task_key_context() -> dict[str, Any] | None:
    """The current task's trusted broker context when it carries a signed identity, else None.

    The broker gives a person's own key only to a task its signed context
    proves works for them; without one, the team's key is used.
    """

    from custom_tools.secure_cli_tool import trusted_broker_context

    try:
        context = trusted_broker_context(allow_system_work=True)
    except Exception:  # noqa: BLE001 - no task context means the team's key, as for work for nobody
        logger.exception("Could not read the task context for a model key lookup. event=model_key_context_failed")
        return None
    if not context or not ({"task_context", "schedule_run"} & context.keys()):
        return None
    return context


def saved_team_endpoints() -> SavedEndpointsV1:
    """The team's endpoints with a saved key, from the broker; raises ``BrokerUnreachable`` or ``BrokerNotConfigured``."""

    return HttpModelKeyBroker().saved_endpoints()


@lru_cache(maxsize=1)
def process_model_keys() -> ModelKeys:
    """This process's model keys (one cache per process)."""

    return ModelKeys(HttpModelKeyBroker())


class EndpointKey:
    """The key of one endpoint, for code that sends its own HTTP requests (embeddings)."""

    def __init__(
        self,
        endpoint: ModelEndpoint,
        keys: ModelKeys,
        *,
        provider: str,
        model_id: str,
        purpose: ConnectionPurposeV1 = ConnectionPurposeV1.CHAT,
    ) -> None:
        self._endpoint = endpoint
        self._keys = keys
        self._purpose = purpose
        self._provider = provider
        self._model_id = model_id
        self._last: ResolvedKey | None = None
        self._context: dict[str, Any] | None = None

    @property
    def address(self) -> str:
        return self._endpoint.address

    def value(self) -> str:
        """The key to send now ("" when the endpoint needs none); raises ``ModelKeyError``."""

        self._context = task_key_context()
        self._last = self._keys.resolve(self._endpoint, self._context, purpose=self._purpose)
        return self._last.value

    def rejected(self, status_code: int | None) -> ModelKeyRejectedError:
        """The provider refused the key last returned: report it and return the error to raise."""

        resolved = self._last or ResolvedKey(address=self._endpoint.address, value="", source=KeySource.NO_KEY_NEEDED)
        self._keys.rejected(
            self._endpoint,
            resolved,
            provider=self._provider,
            model_id=self._model_id,
            status_code=status_code,
            context=self._context,
        )
        return ModelKeyRejectedError(self._endpoint, resolved, status_code)


def embedding_endpoint_key(settings: Any, *, chat_api_base: str | None) -> EndpointKey | None:
    """The key source of a service-backed embedding client (``custom_agents.embedding_service``), or None."""

    if settings.normalized_provider != "openai_compatible" or not settings.api_base:
        return None
    return EndpointKey(
        embedding_endpoint(settings.api_base, chat_api_base=chat_api_base),
        process_model_keys(),
        provider="embedding",
        model_id=str(settings.model_id or ""),
        # Embeddings are the team's service: a person's work keeps them even without the team's model keys.
        purpose=ConnectionPurposeV1.EMBEDDING,
    )


def tell_administrators_about_key_problem(agent: Any, error: ModelKeyError) -> None:
    """One Inbox notice to administrators per endpoint, problem and day: the person was already told.

    The repair (``/auth model``) is a gateway command, so it works even when
    no agent can reach its model.
    """

    from datetime import datetime, timezone

    from access_control.people import PERSON_ROLE_ADMIN
    from custom_agents.access_runtime import send_runtime_notice_once
    from custom_messages.contracts.access_v2 import AccessNoticeKind

    if getattr(error, "source", None) is KeySource.PERSONAL or getattr(error, "persons_own_matter", False):
        return  # the person was told; their own key or model is not the team's problem
    address = str(getattr(error, "address", "") or "unknown")
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    send_runtime_notice_once(
        agent,
        notice_id=f"model_key:{type(error).__name__}:{address}:{day}",
        kind=AccessNoticeKind.MODEL_KEY_PROBLEM,
        title=f"Agents could not use the model key for {address.split('://', 1)[-1]}",
        summary=str(error),
        roles=(PERSON_ROLE_ADMIN,),
    )


# -- Binding keys to a model's calls ------------------------------------------------

_CALL_KEYS: contextvars.ContextVar[Mapping[str, ResolvedKey]] = contextvars.ContextVar("model_call_keys", default={})
_BINDING_MARKER = "_ua_model_key_binding"
# The key of this task's latest model call, for decisions after it failed (failover).
_LAST_CALL_KEY: contextvars.ContextVar[ResolvedKey | None] = contextvars.ContextVar("last_model_call_key", default=None)


def last_call_used_personal_key() -> bool:
    """Whether this task's latest model call used a person's own key.

    Failover switches a whole agent type to another model for a while; a
    person's own key running out (rate limit, credit) must not do that to
    everyone, so ``model_failover_runtime`` skips it then.
    """

    last = _LAST_CALL_KEY.get()
    return last is not None and last.source is KeySource.PERSONAL


def call_key(address: str) -> ResolvedKey | None:
    """The key the current model call resolved for ``address`` (set by ``bind_model_keys``)."""

    return _CALL_KEYS.get().get(address)


class CallKeyedClientArgs(Mapping[str, Any]):
    """LiteLLM client arguments whose ``api_key`` is the current call's key.

    LiteLLM reads ``**client_args`` on every request; this mapping supplies
    the key ``bind_model_keys`` resolved for the call, so the model object
    never holds a key and concurrent calls never share one.
    """

    def __init__(self, base: Mapping[str, Any], address: str) -> None:
        self._base = dict(base)
        self._address = address

    def __getitem__(self, name: str) -> Any:
        if name == "api_key":
            resolved = call_key(self._address)
            if resolved is None:
                raise RuntimeError(
                    f"A model call to {self._address} ran without its key; model calls must go through "
                    "bind_model_keys (custom_agents.model_keys)."
                )
            return resolved.sendable
        return self._base[name]

    def __iter__(self) -> Iterator[str]:
        return iter((*self._base, "api_key"))

    def __len__(self) -> int:
        return len(self._base) + 1


def openai_key_function(endpoint: ModelEndpoint, keys: ModelKeys) -> Callable[[], Awaitable[str]]:
    """An OpenAI-client ``api_key`` function: the current call's key, else a fresh lookup."""

    async def api_key() -> str:
        resolved = call_key(endpoint.address) or await keys.resolve_async(endpoint, task_key_context())
        return resolved.sendable

    return api_key


def bind_model_keys(model: Any, endpoint: ModelEndpoint, keys: ModelKeys, *, provider: str, model_id: str) -> Any:
    """Resolve the endpoint's key before each call of ``model`` and turn a rejected key into ``ModelKeyRejectedError``."""

    for method_name in ("stream", "structured_output"):
        original = getattr(model, method_name, None)
        if original is None or getattr(original, _BINDING_MARKER, False):
            continue
        wrapped = _bound_call(original, endpoint, keys, provider=provider, model_id=model_id)
        setattr(wrapped, _BINDING_MARKER, True)
        setattr(model, method_name, wrapped)
    return model


def _bound_call(
    original: Callable[..., AsyncGenerator[Any, None]],
    endpoint: ModelEndpoint,
    keys: ModelKeys,
    *,
    provider: str,
    model_id: str,
) -> Callable[..., AsyncGenerator[Any, None]]:
    @functools.wraps(original)
    async def call(*args: Any, **kwargs: Any) -> AsyncGenerator[Any, None]:
        context = task_key_context()
        resolved = await keys.resolve_async(endpoint, context)
        # Set for the rest of this task's call; every call sets its own key
        # first, so a key never carries over to another endpoint's call.
        _CALL_KEYS.set({**_CALL_KEYS.get(), endpoint.address: resolved})
        _LAST_CALL_KEY.set(resolved)
        try:
            async for event in original(*args, **kwargs):
                yield event
        except ModelKeyError:
            raise
        except Exception as exc:
            if not is_provider_key_rejected_error(exc):
                raise
            status_code = provider_error_status_code(exc)
            await asyncio.to_thread(
                functools.partial(
                    keys.rejected,
                    endpoint,
                    resolved,
                    provider=provider,
                    model_id=model_id,
                    status_code=status_code,
                    context=context,
                )
            )
            raise ModelKeyRejectedError(endpoint, resolved, status_code) from exc

    return call


__all__ = [
    "BROKER_WAIT_SECONDS",
    "BrokerNotConfigured",
    "CallKeyedClientArgs",
    "EndpointKey",
    "HttpModelKeyBroker",
    "MODEL_KEY_COMMAND",
    "ModelKeyError",
    "ModelKeyMissingError",
    "ModelKeyNotAllowedError",
    "saved_team_endpoints",
    "own_model_needed_message",
    "ModelKeyRejectedError",
    "ModelKeyUnavailableError",
    "ModelKeys",
    "NO_KEY_PLACEHOLDER",
    "RECHECK_SECONDS",
    "ResolvedKey",
    "bind_model_keys",
    "embedding_endpoint_key",
    "call_key",
    "last_call_used_personal_key",
    "openai_key_function",
    "process_model_keys",
    "rejection_message",
    "task_key_context",
    "tell_administrators_about_key_problem",
]
