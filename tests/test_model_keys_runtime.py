"""The agent runtime's model keys (``custom_agents.model_keys``).

A fake broker and an injected clock and environment stand in for the real
ones. Covered: an environment variable overrides the stored key and is
reported by name; a stored key is cached and re-checked; a broker that does
not answer keeps the last key, or is waited for and then explained; a
missing key is explained; a rejected key is forgotten, reported and turned
into a message for the person; LiteLLM and OpenAI-client models get the key
per call; an embedding endpoint uses ``LITELLM_API_KEY`` only at the model
endpoint's address.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from custom_agents.model_keys import (
    BROKER_WAIT_SECONDS,
    NO_KEY_PLACEHOLDER,
    RECHECK_SECONDS,
    BrokerUnreachable,
    CallKeyedClientArgs,
    EndpointKey,
    ModelKeyMissingError,
    ModelKeyRejectedError,
    ModelKeys,
    ModelKeyUnavailableError,
    bind_model_keys,
    openai_key_function,
)
from custom_agents.provider_error_runtime import is_provider_failover_eligible_error, is_provider_key_rejected_error
from custom_agents.turn_failure_runtime import task_runtime_error_message
from custom_messages.contracts.model_keys_v1 import (
    ConnectionPurposeV1,
    KeyReportEvent,
    KeySource,
    ModelKeyReplyV1,
    ModelKeyReportV1,
    StoredKeyState,
)
from model_providers.endpoints import embedding_endpoint, openai_compatible_endpoint

BASE = "https://bifrost.example.com/v1"
ENDPOINT = openai_compatible_endpoint(BASE)


class FakeBroker:
    def __init__(self, key: str | None = "sk-stored-key-0000000001") -> None:
        self.key = key
        self.no_key_needed = False
        self.unreachable = False
        self.lookups = 0
        self.reports: list[ModelKeyReportV1] = []
        self.contexts: list = []
        self.personal: dict[str, str] = {}
        # People whose work may not use the team's keys (access_control.person_settings).
        self.own_model_only: set[str] = set()
        self.purposes: list = []

    def lookup(self, address: str, context=None, *, purpose=ConnectionPurposeV1.CHAT) -> ModelKeyReplyV1:
        self.lookups += 1
        self.contexts.append(context)
        self.purposes.append(purpose)
        if self.unreachable:
            raise BrokerUnreachable("connection refused")
        person = (context or {}).get("canonical_user_id")
        if person in self.personal:
            return ModelKeyReplyV1(address=address, state=StoredKeyState.STORED, key=self.personal[person], owner="person")
        if person in self.own_model_only and purpose is ConnectionPurposeV1.CHAT:
            return ModelKeyReplyV1(address=address, state=StoredKeyState.MISSING, team_keys_allowed=False)
        if self.no_key_needed:
            return ModelKeyReplyV1(address=address, state=StoredKeyState.NO_KEY_NEEDED)
        if self.key is None:
            return ModelKeyReplyV1(address=address, state=StoredKeyState.MISSING)
        return ModelKeyReplyV1(address=address, state=StoredKeyState.STORED, key=self.key)

    def report(self, report: ModelKeyReportV1) -> None:
        self.reports.append(report)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _keys(broker: FakeBroker | None, env: dict[str, str] | None = None, clock: Clock | None = None) -> ModelKeys:
    clock = clock or Clock()
    env = env or {}
    return ModelKeys(broker, env_lookup=env.get, clock=clock, sleep=clock.sleep)


def test_an_environment_variable_overrides_the_stored_key_and_is_reported_by_name(caplog) -> None:
    broker = FakeBroker()
    keys = _keys(broker, {"LITELLM_API_KEY": "sk-env-key-00000000001"})

    resolved = keys.resolve(ENDPOINT)

    assert resolved.value == "sk-env-key-00000000001" and resolved.source is KeySource.ENVIRONMENT
    assert broker.lookups == 0
    assert broker.reports[0].source is KeySource.ENVIRONMENT and broker.reports[0].env_var == "LITELLM_API_KEY"
    assert "sk-env" not in broker.reports[0].model_dump_json()
    assert "event=model_key_environment_override" in caplog.text


def test_a_stored_key_is_cached_and_checked_again_after_a_minute() -> None:
    broker, clock = FakeBroker(), Clock()
    keys = _keys(broker, clock=clock)

    assert keys.resolve(ENDPOINT).value == "sk-stored-key-0000000001"
    broker.key = "sk-stored-key-0000000002"
    assert keys.resolve(ENDPOINT).value == "sk-stored-key-0000000001"
    clock.now += RECHECK_SECONDS

    assert keys.resolve(ENDPOINT).value == "sk-stored-key-0000000002"
    assert broker.lookups == 2
    assert len(broker.reports) == 1, "the source is reported once, not on every call"


def test_a_broker_that_stops_answering_keeps_the_last_key(caplog) -> None:
    broker, clock = FakeBroker(), Clock()
    keys = _keys(broker, clock=clock)
    keys.resolve(ENDPOINT)
    broker.unreachable = True
    clock.now += RECHECK_SECONDS

    assert keys.resolve(ENDPOINT).value == "sk-stored-key-0000000001"
    assert clock.slept == [], "a cached key is used at once, without waiting"
    assert "event=model_key_stale_used" in caplog.text


def test_without_a_cached_key_the_broker_is_waited_for_then_explained() -> None:
    broker, clock = FakeBroker(), Clock()
    broker.unreachable = True
    keys = _keys(broker, clock=clock)

    with pytest.raises(ModelKeyUnavailableError) as raised:
        keys.resolve(ENDPOINT)

    assert 0 < sum(clock.slept) <= BROKER_WAIT_SECONDS
    assert "bifrost.example.com" in str(raised.value) and "broker" in str(raised.value)
    assert "credential store" in str(raised.value)


def test_a_missing_key_says_who_adds_it() -> None:
    with pytest.raises(ModelKeyMissingError) as raised:
        _keys(FakeBroker(key=None)).resolve(ENDPOINT)

    assert "/auth model" in str(raised.value) and "LITELLM_API_KEY" in str(raised.value)
    with pytest.raises(ModelKeyMissingError):
        _keys(None).resolve(ENDPOINT)


def test_no_key_needed_sends_a_placeholder() -> None:
    broker = FakeBroker()
    broker.no_key_needed = True

    resolved = _keys(broker).resolve(ENDPOINT)

    assert resolved.source is KeySource.NO_KEY_NEEDED and resolved.sendable == NO_KEY_PLACEHOLDER


def test_the_embedding_endpoint_uses_the_model_key_variable_only_at_the_same_address() -> None:
    same = embedding_endpoint("https://bifrost.example.com/v1", chat_api_base=BASE)
    other = embedding_endpoint("https://embeddings.example.com/v1", chat_api_base=BASE)

    assert same.env_vars == ("EMBEDDING_API_KEY", "LITELLM_API_KEY")
    assert other.env_vars == ("EMBEDDING_API_KEY",)
    env = {"LITELLM_API_KEY": "sk-model-key-00000000001"}
    assert _keys(FakeBroker(), env).resolve(same).source is KeySource.ENVIRONMENT
    assert _keys(FakeBroker(), env).resolve(other).source is KeySource.STORED


class FakeModel:
    """A Strands-like model whose stream reads the LiteLLM client arguments and may fail."""

    def __init__(self, error: Exception | None = None) -> None:
        self.client_args = CallKeyedClientArgs({"api_base": BASE}, ENDPOINT.address)
        self.error = error
        self.keys_seen: list[str] = []

    async def stream(self, *_args: Any, **_kwargs: Any):
        self.keys_seen.append(dict(self.client_args)["api_key"])
        if self.error is not None:
            raise self.error
        yield {"event": "ok"}


class AuthenticationError(Exception):
    status_code = 401


async def _drain(model: Any) -> list[Any]:
    return [event async for event in model.stream([])]


def test_each_model_call_gets_the_current_key() -> None:
    broker, clock = FakeBroker(), Clock()
    keys = _keys(broker, clock=clock)
    model = bind_model_keys(FakeModel(), ENDPOINT, keys, provider="litellm", model_id="m")

    asyncio.run(_drain(model))
    broker.key = "sk-stored-key-0000000002"
    clock.now += RECHECK_SECONDS
    asyncio.run(_drain(model))

    assert model.keys_seen == ["sk-stored-key-0000000001", "sk-stored-key-0000000002"]


def test_a_rejected_key_is_forgotten_reported_and_explained() -> None:
    broker = FakeBroker()
    keys = _keys(broker)
    model = bind_model_keys(FakeModel(AuthenticationError("Incorrect API key provided")), ENDPOINT, keys, provider="litellm", model_id="m")

    with pytest.raises(ModelKeyRejectedError) as raised:
        asyncio.run(_drain(model))

    assert "rejected the team's key" in str(raised.value) and "/auth model" in str(raised.value)
    rejection = broker.reports[-1]
    assert rejection.event is KeyReportEvent.REJECTED and rejection.status_code == 401 and rejection.model_id == "m"
    model.error = None
    asyncio.run(_drain(model))
    assert broker.lookups == 2, "the next call asks the broker again instead of reusing the rejected key"
    wrapped = RuntimeError("event loop failed")
    wrapped.__cause__ = raised.value
    assert task_runtime_error_message(wrapped) == str(raised.value)


def test_an_environment_key_rejection_says_to_change_the_variable() -> None:
    keys = _keys(FakeBroker(), {"LITELLM_API_KEY": "sk-env-key-00000000001"})
    model = bind_model_keys(FakeModel(AuthenticationError("401")), ENDPOINT, keys, provider="litellm", model_id="m")

    with pytest.raises(ModelKeyRejectedError) as raised:
        asyncio.run(_drain(model))

    assert "LITELLM_API_KEY" in str(raised.value) and "restarts the container" in str(raised.value)


def test_other_provider_errors_pass_through_unchanged() -> None:
    model = bind_model_keys(FakeModel(RuntimeError("rate limit")), ENDPOINT, _keys(FakeBroker()), provider="litellm", model_id="m")

    with pytest.raises(RuntimeError, match="rate limit"):
        asyncio.run(_drain(model))


def test_a_client_argument_read_outside_a_bound_call_fails_loudly() -> None:
    with pytest.raises(RuntimeError, match="bind_model_keys"):
        CallKeyedClientArgs({"api_base": BASE}, ENDPOINT.address)["api_key"]


def test_the_openai_client_key_function_looks_the_key_up() -> None:
    key_function = openai_key_function(ENDPOINT, _keys(FakeBroker()))

    assert asyncio.run(key_function()) == "sk-stored-key-0000000001"


def test_an_embedding_key_reports_a_rejection() -> None:
    broker = FakeBroker()
    key = EndpointKey(ENDPOINT, _keys(broker), provider="embedding", model_id="embed-1")

    assert key.value() == "sk-stored-key-0000000001"
    error = key.rejected(401)

    assert isinstance(error, ModelKeyRejectedError)
    assert broker.reports[-1].event is KeyReportEvent.REJECTED


def test_a_key_rejection_is_classified_and_never_fails_over() -> None:
    assert is_provider_key_rejected_error(AuthenticationError("x"))
    assert is_provider_key_rejected_error(RuntimeError("Error: API key not valid. Please pass a valid API key."))
    assert not is_provider_key_rejected_error(RuntimeError("HTTP 403: this model is not allowed"))
    assert not is_provider_failover_eligible_error(AuthenticationError("Incorrect API key provided"))


def test_a_process_without_a_broker_says_the_key_is_missing_without_waiting(monkeypatch) -> None:
    from custom_agents.model_keys import HttpModelKeyBroker

    monkeypatch.delenv("SECURE_TOOL_BROKER_INTERNAL_BASE_URL", raising=False)
    clock = Clock()
    keys = ModelKeys(HttpModelKeyBroker(), env_lookup={}.get, clock=clock, sleep=clock.sleep)

    with pytest.raises(ModelKeyMissingError):
        keys.resolve(ENDPOINT)
    assert clock.slept == []



ALICE = {"canonical_user_id": "alice", "task_context": {"signed": "by the gateway"}}
BOB = {"canonical_user_id": "bob", "task_context": {"signed": "by the gateway"}}


def test_a_persons_own_key_is_used_only_for_their_work_and_never_reported_as_the_teams() -> None:
    broker = FakeBroker()
    broker.personal["alice"] = "sk-alice-own-key-00001"
    keys = _keys(broker)

    alice = keys.resolve(ENDPOINT, ALICE)
    bob = keys.resolve(ENDPOINT, BOB)
    nobody = keys.resolve(ENDPOINT)

    assert (alice.value, alice.source) == ("sk-alice-own-key-00001", KeySource.PERSONAL)
    assert (bob.value, bob.source) == ("sk-stored-key-0000000001", KeySource.STORED)
    assert nobody.source is KeySource.STORED
    assert broker.contexts == [ALICE, BOB, None]
    assert all(report.source is not KeySource.PERSONAL for report in broker.reports)


def test_a_rejected_personal_key_stops_the_call_and_tells_that_person_not_the_team(monkeypatch) -> None:
    from custom_agents import model_keys as module

    broker = FakeBroker()
    broker.personal["alice"] = "sk-alice-own-key-00001"
    keys = _keys(broker)
    monkeypatch.setattr(module, "task_key_context", lambda: ALICE)
    model = bind_model_keys(FakeModel(AuthenticationError("Incorrect API key")), ENDPOINT, keys, provider="litellm", model_id="m")

    with pytest.raises(ModelKeyRejectedError) as raised:
        asyncio.run(_drain(model))

    assert "rejected your own key" in str(raised.value) and "/auth model me" in str(raised.value)
    assert model.keys_seen == ["sk-alice-own-key-00001"], "no fallback to the team's key"
    assert broker.reports == [], "a person's own key is not the team's key state"
    notices = []
    monkeypatch.setattr("custom_agents.access_runtime.send_runtime_notice_once", lambda *a, **k: notices.append(k))
    module.tell_administrators_about_key_problem(object(), raised.value)
    assert notices == []


def test_a_personal_keys_rate_limit_never_switches_the_team_to_a_fallback(monkeypatch, caplog) -> None:
    from custom_agents import model_failover_runtime
    from custom_agents import model_keys as module

    broker = FakeBroker()
    broker.personal["alice"] = "sk-alice-own-key-00001"
    monkeypatch.setattr(module, "task_key_context", lambda: ALICE)
    monkeypatch.setattr(model_failover_runtime, "is_enabled", lambda agent: True)
    monkeypatch.setattr(model_failover_runtime, "is_failover_eligible_error", lambda agent, error: True)
    model = bind_model_keys(FakeModel(RuntimeError("rate limit")), ENDPOINT, _keys(broker), provider="litellm", model_id="m")

    async def turn():
        with pytest.raises(RuntimeError):
            await _drain(model)
        return await model_failover_runtime.handle_provider_failure(
            object(), error=RuntimeError("rate limit"), task_id="t1", tool_calls_started=False
        )

    assert asyncio.run(turn()) is None
    assert "event=model_failover_skipped_personal_key" in caplog.text


def test_work_for_a_person_without_the_team_keys_is_refused_even_with_an_environment_override() -> None:
    from custom_agents.model_keys import ModelKeyNotAllowedError

    broker = FakeBroker()
    broker.own_model_only.add("alice")
    keys = _keys(broker, {"LITELLM_API_KEY": "sk-env-key-0000000009"})

    with pytest.raises(ModelKeyNotAllowedError) as refused:
        keys.resolve(ENDPOINT, ALICE)

    assert str(refused.value).startswith("You haven't set up your own model yet. Use /auth model me.")
    # Others, and work for nobody, keep the team's key and its environment override.
    assert keys.resolve(ENDPOINT, BOB).source is KeySource.ENVIRONMENT
    assert keys.resolve(ENDPOINT).source is KeySource.ENVIRONMENT


def test_a_persons_own_key_wins_over_an_environment_override_and_embeddings_keep_the_teams_key() -> None:
    broker = FakeBroker()
    broker.personal["bob"] = "sk-bob-own-key-000001"
    broker.own_model_only.add("alice")
    keys = _keys(broker, {"LITELLM_API_KEY": "sk-env-key-0000000009"})

    assert keys.resolve(ENDPOINT, BOB).source is KeySource.PERSONAL
    embedding = keys.resolve(ENDPOINT, ALICE, purpose=ConnectionPurposeV1.EMBEDDING)

    assert embedding.source is KeySource.ENVIRONMENT
    assert broker.purposes[-1] is ConnectionPurposeV1.EMBEDDING
