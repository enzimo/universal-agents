"""Opt-in live validation of OpenRouter model parameter metadata."""

from __future__ import annotations

import json
import os
from urllib import request as urllib_request

import pytest

from custom_agents.reasoning_litellm_model import ReasoningAwareLiteLLMModel
from custom_agents.strands_model_factory import StrandsModelFactoryConfig, build_strands_model


OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
OPENROUTER_GPT_5_6_LUNA_MODEL_ID = "openai/gpt-5.6-luna"

pytestmark = [
    pytest.mark.live,
    pytest.mark.external,
    pytest.mark.skipif(
        os.getenv("RUN_LIVE_OPENROUTER_MODEL_CONTRACT") != "1",
        reason=(
            "Set RUN_LIVE_OPENROUTER_MODEL_CONTRACT=1 to validate current OpenRouter "
            "model metadata."
        ),
    ),
]


def test_live_openrouter_gpt_5_6_luna_request_matches_supported_parameters() -> None:
    with urllib_request.urlopen(OPENROUTER_MODELS_URL, timeout=30) as response:
        payload = json.load(response)
    model_metadata = next(
        (
            item
            for item in payload.get("data", [])
            if item.get("id") == OPENROUTER_GPT_5_6_LUNA_MODEL_ID
        ),
        None,
    )
    assert model_metadata is not None
    supported_parameters = set(model_metadata.get("supported_parameters") or [])
    assert "reasoning_effort" in supported_parameters
    assert "max_completion_tokens" in supported_parameters
    assert "tools" in supported_parameters
    assert "temperature" not in supported_parameters

    model = build_strands_model(
        StrandsModelFactoryConfig(
            provider="litellm",
            model_id=OPENROUTER_GPT_5_6_LUNA_MODEL_ID,
            temperature=0.0,
            reasoning_effort="medium",
            max_output_tokens=1_024,
            litellm_api_base="https://openrouter.ai/api/v1",
            litellm_api_key="contract-test-key",
        )
    )

    assert isinstance(model, ReasoningAwareLiteLLMModel)
    assert set(model.config["params"]) <= supported_parameters
    assert model.config["params"] == {
        "max_completion_tokens": 1_024,
        "reasoning_effort": "medium",
    }
