from strands.types.exceptions import MaxTokensReachedException

from custom_agents.provider_error_runtime import (
    PROVIDER_TIMEOUT_USER_MESSAGE,
    ProviderTimeoutError,
)
from custom_agents.task_message_runtime import _task_error_response_errors
from custom_agents.turn_failure_runtime import task_runtime_error_message


def test_max_tokens_error_is_safe_and_actionable() -> None:
    error = MaxTokensReachedException(
        "Partial message was retained. See https://strandsagents.com/internal-details"
    )

    rendered = task_runtime_error_message(error)

    assert "output limit" in rendered
    assert "split it into steps" in rendered
    assert "strandsagents.com" not in rendered
    assert "Partial message was retained" not in rendered


def test_provider_timeout_error_is_safe_and_retryable() -> None:
    rendered = task_runtime_error_message(ProviderTimeoutError())

    assert rendered == PROVIDER_TIMEOUT_USER_MESSAGE
    assert "Please try again" in rendered
    assert "Runtime error:" not in rendered


def test_error_response_replaces_timestamped_terminal_duplicate() -> None:
    rendered = _task_error_response_errors(
        [
            "Earlier task warning.",
            "2026-08-22T03:56:02.379021+00:00: "
            + PROVIDER_TIMEOUT_USER_MESSAGE,
        ],
        PROVIDER_TIMEOUT_USER_MESSAGE,
    )

    assert rendered == ["Earlier task warning.", PROVIDER_TIMEOUT_USER_MESSAGE]
