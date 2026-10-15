"""Which model endpoints the team's configuration uses, for ``/auth model`` and the Status page.

The broker stores keys but cannot read the instance configuration, so the
gateway lists the endpoints: one per provider in use, with the model to test
its key with (the first configured model on it, in this order: the workspace
default from ``LLM_OVERRIDES.yaml``, ``llm.*``, the per-type overrides in
``LLM_OVERRIDES.yaml`` and ``instance_config.yaml``, then failover
fallbacks):

- the OpenAI-compatible endpoint (``llm.litellm_api_base``) when a model uses
  the ``litellm`` provider;
- each well-known provider in use (OpenAI, Anthropic, Gemini, xAI), at its
  fixed address;
- an OpenAI-compatible embedding service (``embedding.api_base``).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from assistant_gateway.config import GatewayInstanceConfig
from custom_messages.contracts.model_keys_v1 import ConnectionPurposeV1, ModelKeyEndpointUseV1
from model_providers.endpoints import (
    PROVIDER_ENDPOINT_KINDS,
    WELL_KNOWN_PROVIDERS,
    EndpointKind,
    InvalidEndpointAddress,
    checked_endpoint_url,
)

LOGGER = logging.getLogger(__name__)
_OPENAI_COMPATIBLE_EMBEDDINGS = "openai_compatible"


def configured_model_endpoints(config: GatewayInstanceConfig) -> tuple[ModelKeyEndpointUseV1, ...]:
    """The endpoints to offer on ``/auth model``; an invalid address is logged and left out.

    Read from the saved file, not the gateway's start-up copy: the endpoint
    can be switched while the gateway runs (``custom_agents.endpoint_switch``).
    """

    config = _saved(config)
    first_models: dict[EndpointKind, tuple[str, str]] = {}
    for entry in _model_entries(config):
        kind = PROVIDER_ENDPOINT_KINDS.get(str(entry.get("provider") or entry.get("model_provider") or "").strip().lower())
        model_id = str(entry.get("model_id") or "").strip()
        if kind is not None and model_id and kind not in first_models:
            first_models[kind] = (model_id, str(entry.get("litellm_api_mode") or config.llm.litellm_api_mode or ""))
    uses: list[ModelKeyEndpointUseV1] = []
    for kind, (model_id, api_mode) in first_models.items():
        if kind is EndpointKind.OPENAI_COMPATIBLE:
            if config.llm.litellm_api_base:
                _append(uses, config.llm.litellm_api_base, kind, ConnectionPurposeV1.CHAT, model_id, api_mode, "Team model endpoint")
        else:
            provider = WELL_KNOWN_PROVIDERS[kind]
            _append(uses, provider.base_url, kind, ConnectionPurposeV1.CHAT, model_id, "", provider.label)
    embedding = config.embedding
    if str(embedding.provider or "").lower() == _OPENAI_COMPATIBLE_EMBEDDINGS and embedding.api_base:
        _append(
            uses, embedding.api_base, EndpointKind.OPENAI_COMPATIBLE, ConnectionPurposeV1.EMBEDDING,
            str(embedding.model_id or ""), "", "Embeddings",
        )
    return tuple(uses)


def _model_entries(config: GatewayInstanceConfig) -> Iterable[Mapping[str, Any]]:
    overrides = _llm_overrides(config.layout.llm_overrides_path)
    if isinstance(overrides.get("default"), Mapping):
        yield overrides["default"]
    yield {"provider": config.llm.provider, "model_id": config.llm.model_id}
    agent_types = overrides.get("agent_types")
    if isinstance(agent_types, Mapping):
        yield from (entry for entry in agent_types.values() if isinstance(entry, Mapping))
    for settings in config.agent_types.values():
        if settings.llm is not None:
            yield settings.llm.model_dump()
    fallbacks = (config.llm.failover or {}).get("fallbacks") or ()
    yield from (entry for entry in fallbacks if isinstance(entry, Mapping))


def _saved(config: GatewayInstanceConfig) -> GatewayInstanceConfig:
    try:
        return GatewayInstanceConfig.load_saved(config.config_path)
    except Exception as exc:  # noqa: BLE001 - the start-up copy is the last known configuration
        LOGGER.warning(
            "The saved instance configuration could not be read; using the gateway's start-up copy. "
            "event=model_endpoints_saved_config_unreadable path=%s error=%s",
            config.config_path,
            exc,
        )
        return config


def _append(
    uses: list[ModelKeyEndpointUseV1],
    base_url: str,
    kind: EndpointKind,
    purpose: ConnectionPurposeV1,
    model_id: str,
    api_mode: str,
    label: str,
) -> None:
    try:
        checked = checked_endpoint_url(base_url)
    except InvalidEndpointAddress as exc:
        LOGGER.warning(
            "A configured model endpoint address is not usable. event=model_endpoint_address_invalid purpose=%s "
            "error=%s",
            purpose.value,
            exc,
        )
        return
    uses.append(
        ModelKeyEndpointUseV1(
            base_url=checked,
            kind=kind,
            purpose=purpose,
            model_id=model_id,
            api_mode=api_mode or "chat_completions",
            label=label,
        )
    )


def _llm_overrides(path: Path) -> Mapping[str, Any]:
    if not path.is_file():
        return {}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        LOGGER.warning("LLM_OVERRIDES.yaml could not be read. event=llm_overrides_unreadable error=%s", exc)
        return {}
    return loaded if isinstance(loaded, Mapping) else {}


__all__ = ["configured_model_endpoints"]
