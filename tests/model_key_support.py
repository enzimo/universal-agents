"""Model keys for tests: a ``ModelKeys`` whose only source is a fixed environment value."""

from __future__ import annotations

from custom_agents.model_keys import ModelKeys
from model_providers.endpoints import LITELLM_API_KEY_ENV


def env_model_keys(key: str, *, env_var: str = LITELLM_API_KEY_ENV) -> ModelKeys:
    """Keys as if ``env_var`` held ``key`` and no broker were reachable."""

    return ModelKeys(None, env_lookup={env_var: key}.get)
