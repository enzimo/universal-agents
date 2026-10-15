"""Switching the team's OpenAI-compatible model endpoint (``llm.litellm_api_base``).

Why this needs its own checks: every agent on the ``litellm`` provider sends
its model requests, with the conversation in them, to this address. A wrong
address leaves every agent without a model from its next turn, and nobody
can ask an agent to fix it. So, in order:

1. **Staging is refused** when ``LITELLM_API_BASE`` set by the operator would
   win over the saved value anyway (the launcher's list of overrides,
   ``InstanceLayout.environment_overrides_path``, or the variable in a process
   started without the launcher), and when no key exists for the new address
   (``custom_agents.model_keys``): an administrator saves it first with
   ``/auth model`` → Another Address. Staging also sends one test request,
   trying the address with and without ``/v1``
   (``model_providers.connection_test.check_endpoint_address``), and stages
   the form that answered: people name an endpoint by its host, and the
   wrong path would leave every agent without a model after the restart.
2. **The Inbox text** says where the key will come from. Keys belong to an
   address, so the old endpoint's stored key is never sent to the new one;
   only ``LITELLM_API_KEY``, an operator override, would be, and the text
   says so.
3. **Before saving**, one real request goes to the new address with its key
   and the model the team uses there (``model_providers.connection_test``).
   A failure saves nothing.

The change itself is an ordinary runtime settings plan
(``RuntimeSettingsService``), approved by an administrator in the Inbox. It
needs no restart: every agent picks up the saved endpoint at the start of its
next turn (``custom_agents.endpoint_refresh``).
"""

from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import urlsplit

from pydantic import ValidationError

from custom_agents.model_keys import (
    MODEL_KEY_COMMAND,
    BrokerNotConfigured,
    BrokerUnreachable,
    ModelKeyError,
    ModelKeyMissingError,
    process_model_keys,
    saved_team_endpoints,
)
from custom_messages.contracts.model_keys_v1 import SavedEndpointV1
from custom_messages.contracts.model_keys_v1 import KeySource
from custom_messages.contracts.status_v1 import EnvironmentOverridesV1
from model_providers.connection_test import (
    CONNECTION_TEST_TIMEOUT_SECONDS,
    ConnectionTestRequest,
    check_endpoint_address,
    failure_explanation,
    run_connection_test,
)
from model_providers.endpoints import EndpointKind, openai_compatible_endpoint

logger = logging.getLogger(__name__)

ENDPOINT_SETTING_PATH = "llm.litellm_api_base"
ENDPOINT_ENV_VAR = "LITELLM_API_BASE"
_LITELLM = "litellm"


class EndpointSwitchRefused(ValueError):
    """The endpoint cannot be switched as asked; the message says why and what to do."""


def check_endpoint_change(agent: Any, config: Any, new_base_url: str) -> str:
    """The address to stage: refused when an override would win, the address has no key or does not answer (module docstring)."""

    if operator_overrides_endpoint(config):
        raise EndpointSwitchRefused(
            f"{ENDPOINT_ENV_VAR} is set in team.env or the container environment, so it would keep overriding "
            "the saved endpoint. The operator removes it there and restarts the container, then the endpoint "
            "can be switched here."
        )
    endpoint = openai_compatible_endpoint(new_base_url)
    saved = _saved_endpoints()
    try:
        key = process_model_keys().resolve(endpoint).value
    except ModelKeyMissingError as exc:
        raise EndpointSwitchRefused(
            f"There is no key for {endpoint.host} yet. An administrator saves it first with {MODEL_KEY_COMMAND} "
            f"(New OpenAI-compatible address; it is tested there), then ask again.{_saved_hint(saved)}"
        ) from exc
    except ModelKeyError as exc:
        raise EndpointSwitchRefused(str(exc)) from exc
    return _answering_address(agent, config, _saved_form(new_base_url, endpoint.address, saved), key)


def _saved_endpoints() -> tuple[SavedEndpointV1, ...]:
    """The team's endpoints with saved keys; empty (logged) when the broker cannot say."""

    try:
        return saved_team_endpoints().endpoints
    except (BrokerUnreachable, BrokerNotConfigured, RuntimeError, ValueError) as exc:
        logger.warning("Could not list the team's saved model endpoints. event=saved_model_endpoints_unavailable error=%s", exc)
        return ()


def _saved_form(new_base_url: str, address: str, saved: tuple[SavedEndpointV1, ...]) -> str:
    """A bare host becomes the address form its saved key answered at (``/v1`` or not); a typed path is kept."""

    if urlsplit(new_base_url).path.strip("/"):
        return new_base_url
    match = next((item for item in saved if item.address == address and item.base_url), None)
    return match.base_url if match is not None else new_base_url


def _saved_hint(saved: tuple[SavedEndpointV1, ...]) -> str:
    compatible = [item for item in saved if item.kind is EndpointKind.OPENAI_COMPATIBLE]
    if not compatible:
        return ""
    return " Endpoints with a saved key: " + ", ".join(item.base_url or item.address for item in compatible) + "."


def _answering_address(agent: Any, config: Any, new_base_url: str, key: str) -> str:
    """The form of the address (with or without ``/v1``) that answers a test request."""

    model = _model_on_endpoint(agent, config)
    if model is None:
        return new_base_url
    model_id, api_mode = model
    outcome = check_endpoint_address(
        ConnectionTestRequest(
            kind=EndpointKind.OPENAI_COMPATIBLE,
            base_url=new_base_url,
            key=key,
            model=model_id,
            api_mode=api_mode,
            timeout_seconds=_test_timeout(agent, config),
        )
    )
    if not outcome.ok:
        logger.warning(
            "The new model endpoint failed its staging test. event=endpoint_switch_stage_test_failed "
            "address=%s model=%s status_code=%s",
            openai_compatible_endpoint(new_base_url).address,
            model_id,
            outcome.result.status_code,
        )
        raise EndpointSwitchRefused(
            f"The test request to {openai_compatible_endpoint(new_base_url).host} with {model_id} failed. "
            f"{failure_explanation(outcome)} The endpoint's answer: {outcome.result.message}"
        )
    if outcome.moved:
        logger.info(
            "The new model endpoint answers at the other form of its address. event=endpoint_switch_address_adjusted "
            "typed=%s staged=%s",
            new_base_url,
            outcome.base_url,
        )
    return outcome.base_url


def endpoint_change_effect(new_base_url: str) -> str:
    """The Inbox line saying where requests and which key will go (never the key)."""

    endpoint = openai_compatible_endpoint(new_base_url)
    try:
        resolved = process_model_keys().resolve(endpoint)
    except ModelKeyError as exc:
        return f"Agents' model requests, with their conversations, will go to {endpoint.host}. Its key: {exc}"
    key_text = {
        KeySource.STORED: f"the key saved for {endpoint.host} with {MODEL_KEY_COMMAND}",
        KeySource.NO_KEY_NEEDED: "no key (it was saved as needing none)",
        KeySource.ENVIRONMENT: (
            f"the key in {resolved.env_var}, an environment override, which will then be sent to {endpoint.host}"
        ),
    }[resolved.source]
    return f"Agents' model requests, with their conversations, will go to {endpoint.host}, using {key_text}."


def check_endpoint_with_a_request(agent: Any, config: Any, new_base_url: str) -> dict[str, Any]:
    """Send one request to the new address with its key; ``{"error": ...}`` when it fails (nothing is saved then)."""

    endpoint = openai_compatible_endpoint(new_base_url)
    model = _model_on_endpoint(agent, config)
    if model is None:
        return {"status": "skipped", "reason": "no configured model uses the OpenAI-compatible endpoint"}
    model_id, api_mode = model
    try:
        key = process_model_keys().resolve(endpoint).value
    except ModelKeyError as exc:
        return {"error": str(exc), "phase": "preflight"}
    result = run_connection_test(
        ConnectionTestRequest(
            kind=EndpointKind.OPENAI_COMPATIBLE,
            base_url=new_base_url,
            key=key,
            model=model_id,
            api_mode=api_mode,
            timeout_seconds=_test_timeout(agent, config),
        )
    )
    if not result.ok:
        logger.warning(
            "The new model endpoint failed its test; nothing was saved. event=endpoint_switch_test_failed "
            "address=%s model=%s status_code=%s",
            endpoint.address,
            model_id,
            result.status_code,
        )
        return {
            "error": f"Nothing was changed. The test request to {endpoint.host} with {model_id} failed: {result.message}",
            "phase": "preflight",
        }
    return {"status": "passed", "tested_before_write": True, "address": endpoint.address, "model_id": model_id}


def _test_timeout(agent: Any, config: Any) -> float:
    """The wait agents' own model requests use (llm.request_timeout_seconds), so a slow but working model passes."""

    configured = getattr(agent, "request_timeout_seconds", None) or getattr(config.llm, "request_timeout_seconds", None)
    return float(configured or CONNECTION_TEST_TIMEOUT_SECONDS)


def _model_on_endpoint(agent: Any, config: Any) -> tuple[str, str] | None:
    """(model, API mode) to test with: this agent's when it uses the endpoint, else the team default's."""

    if str(getattr(agent, "model_provider", "") or "").lower() == _LITELLM and getattr(agent, "model_id", None):
        return str(agent.model_id), str(getattr(agent, "litellm_api_mode", "") or "chat_completions")
    if str(config.llm.provider or "").lower() == _LITELLM and config.llm.model_id:
        return str(config.llm.model_id), str(config.llm.litellm_api_mode or "chat_completions")
    return None


def operator_overrides_endpoint(config: Any) -> bool:
    """Whether ``LITELLM_API_BASE`` set by the operator wins over the saved endpoint (also read by ``endpoint_refresh``)."""

    path = config.layout.environment_overrides_path
    if path.is_file():
        try:
            overrides = EnvironmentOverridesV1.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError) as exc:
            logger.warning(
                "The launcher's list of environment overrides is not readable. event=environment_overrides_unreadable "
                "path=%s error=%s",
                path,
                exc,
            )
        else:
            return ENDPOINT_ENV_VAR in overrides.setting_variables
    # Started without the launcher: a variable that differs from the saved value is the operator's.
    value = str(os.getenv(ENDPOINT_ENV_VAR) or "").strip()
    return bool(value) and value.rstrip("/") != str(config.llm.litellm_api_base or "").rstrip("/")


__all__ = [
    "ENDPOINT_SETTING_PATH",
    "EndpointSwitchRefused",
    "check_endpoint_change",
    "endpoint_change_effect",
    "check_endpoint_with_a_request",
    "operator_overrides_endpoint",
]
