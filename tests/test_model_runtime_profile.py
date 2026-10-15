
import pytest
from pydantic import ValidationError

from custom_agents.model_runtime_profile import (
    RuntimeLLMProfile,
)


def test_runtime_profile_rejects_too_small_context_window() -> None:
    with pytest.raises(ValidationError, match="at least 1024"):
        RuntimeLLMProfile(
            provider="litellm",
            model_id="test/model",
            context_window_tokens=512,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("reasoning_summary", "raw"),
        ("litellm_api_mode", "automatic"),
    ],
)
def test_runtime_profile_rejects_invalid_request_options(
    field: str, value: str
) -> None:
    with pytest.raises(ValidationError):
        RuntimeLLMProfile(
            provider="litellm",
            model_id="test/model",
            **{field: value},
        )
