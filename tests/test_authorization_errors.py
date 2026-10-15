from __future__ import annotations

import pytest

from assistant_gateway.application.authorization_errors import (
    present_authorization_error,
)
from custom_messages.contracts.authorization_presentation_mode import (
    AuthorizationPresentationMode,
)


@pytest.mark.parametrize(
    ("error", "code", "status", "recovery_text"),
    [
        (
            PermissionError("Only an administrator can make this change."),
            "access_denied",
            403,
            "team administrator",
        ),
        (
            RuntimeError("gateway transport is offline"),
            "authorization_service_unavailable",
            503,
            "try again",
        ),
        (
            RuntimeError("worker returned an invalid record"),
            "authorization_action_failed",
            502,
            "Refresh the current state",
        ),
        (
            ValueError("MCP connection decision must be approve or reject."),
            "invalid_authorization_request",
            400,
            "review the current choices",
        ),
    ],
)
def test_control_errors_explain_failure_and_recovery_without_raw_details(
    error: Exception,
    code: str,
    status: int,
    recovery_text: str,
) -> None:
    presentation = present_authorization_error(error, operation="approve this MCP server")
    payload = presentation.payload()["error"]

    assert presentation.code == code
    assert presentation.status_code == status
    assert "approve this MCP server" in str(payload["message"])
    assert recovery_text in str(payload["recovery"])
    assert str(error) not in str(payload)


def test_technical_error_adds_stable_details_without_raw_exception_text() -> None:
    error = RuntimeError("worker returned secret backend payload token=do-not-show")
    presentation = present_authorization_error(
        error,
        operation="add this procedure",
        presentation_mode=AuthorizationPresentationMode.TECHNICAL,
    )
    payload = presentation.payload()["error"]

    assert payload["technical"] == {
        "operation": "add this procedure",
        "status_code": 502,
        "failure_type": "RuntimeError",
    }
    assert "code=authorization_action_failed" in presentation.user_message()
    assert "token=do-not-show" not in str(payload)
    assert "token=do-not-show" not in presentation.user_message()
