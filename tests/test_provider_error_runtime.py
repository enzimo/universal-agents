from tests.llm_turn_fixtures import turn_executor_for
from types import SimpleNamespace
from io import BytesIO
from urllib.error import HTTPError

import httpx
import openai
import pytest
from strands.types.exceptions import EventLoopException, ModelThrottledException

from custom_agents.model_keys import HttpModelKeyBroker, ModelKeys, ModelKeyUnavailableError
from custom_agents.provider_error_runtime import is_provider_timeout_error
from custom_agents.turn_failure_runtime import task_runtime_error_message
from model_providers.endpoints import openai_compatible_endpoint


from custom_agents import model_failover_runtime
from custom_agents.provider_error_runtime import (
    ProviderConnectivityError,
    is_provider_connectivity_error,
    is_provider_failover_eligible_error,
    is_provider_overload_error,
)


def _litellm_refused_connection() -> BaseException:
    refused = ConnectionRefusedError(111, "Connect call failed ('127.0.0.1', 4000)")
    api_connection = RuntimeError("openai.APIConnectionError: Connection error.")
    api_connection.__cause__ = refused
    proxy_error = RuntimeError(
        "litellm.InternalServerError: InternalServerError: "
        "Litellm_proxyException - Connection error."
    )
    proxy_error.__cause__ = api_connection
    return proxy_error


def test_litellm_refused_connection_is_connectivity_not_overload() -> None:
    error = _litellm_refused_connection()

    assert is_provider_connectivity_error(error) is True
    assert is_provider_overload_error(error) is False
    assert turn_executor_for(None)._is_provider_connectivity_error(error) is True
    assert turn_executor_for(None)._is_provider_overload_error(error) is False


def test_litellm_refused_connection_remains_failover_eligible() -> None:
    error = _litellm_refused_connection()

    assert is_provider_failover_eligible_error(error) is True
    assert model_failover_runtime.is_failover_eligible_error(object(), error) is True


def test_connectivity_failure_message_is_actionable_for_docker_localhost() -> None:
    agent = SimpleNamespace(
        model_provider="litellm",
        model_id="fireworks/minimax-m3",
        litellm_api_base="http://localhost:4000",
    )

    rendered = turn_executor_for(agent)._provider_connectivity_failure(_litellm_refused_connection())

    assert isinstance(rendered, ProviderConnectivityError)
    assert "connectivity/configuration failure" in str(rendered)
    assert "litellm/fireworks/minimax-m3" in str(rendered)
    assert "establishing the provider connection" in str(rendered)
    assert "llm.litellm_api_base" in str(rendered)
    assert "localhost is the UA container itself" in str(rendered)
    assert "127.0.0.1:4000" not in str(rendered)


def test_connectivity_failure_message_does_not_claim_remote_endpoint_is_localhost() -> None:
    agent = SimpleNamespace(
        model_provider="litellm",
        model_id="fireworks/minimax-m3",
        litellm_api_base="https://litellm.example.test",
    )

    rendered = turn_executor_for(agent)._provider_connectivity_failure(_litellm_refused_connection())

    assert "llm.litellm_api_base" in str(rendered)
    assert "localhost is the UA container itself" not in str(rendered)


@pytest.mark.parametrize("status", [500, 503, 504])
@pytest.mark.parametrize("wrapper", ["direct", "strands", "openai", "throttled"])
def test_broker_credential_errors_never_enter_provider_handling(status, wrapper, monkeypatch):
    endpoint = openai_compatible_endpoint("https://model.example/v1")
    monkeypatch.setenv("SECURE_TOOL_BROKER_INTERNAL_BASE_URL", "http://broker.example")
    monkeypatch.setenv("SECURE_TOOL_BROKER_TOKEN", "test-caller-token")

    def fail_lookup(request, *, timeout):
        detail = b"The broker could not read the model keys from its credential store. connection refused; request timed out"
        raise HTTPError(request.full_url, status, "Service Unavailable", {}, BytesIO(detail))

    monkeypatch.setattr("custom_agents.model_keys.urlopen", fail_lookup)
    keys = ModelKeys(HttpModelKeyBroker(), env_lookup={}.get, sleep=lambda _delay: None)
    with pytest.raises(ModelKeyUnavailableError) as raised:
        keys.resolve(endpoint)
    local_error = raised.value
    if wrapper == "strands":
        error = EventLoopException(local_error)
    elif wrapper == "throttled":
        error = ModelThrottledException(f"Error code: {status}: service unavailable")
        error.__context__ = local_error
    elif wrapper == "openai":
        response = httpx.Response(status, request=httpx.Request("POST", endpoint.address))
        error = openai.InternalServerError(f"Error code: {status}: service unavailable", response=response, body=None)
        error.__cause__ = local_error
    else:
        error = local_error

    assert not is_provider_overload_error(error)
    assert not is_provider_connectivity_error(error)
    assert not is_provider_timeout_error(error)
    assert not is_provider_failover_eligible_error(error)
    assert not model_failover_runtime.is_failover_eligible_error(object(), error)
    assert task_runtime_error_message(error) == str(local_error)
    assert "credential store" in str(local_error)
    assert "administrator" in str(local_error)
    assert "broker did not answer" not in str(local_error)
