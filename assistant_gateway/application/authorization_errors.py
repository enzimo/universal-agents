"""Safe, actionable errors for the web chat's platform controls.

The MCP controls and installed-procedure reviews in Settings return these
bodies when an action is refused or fails, worded for people; the raw
exception text only goes to the log. ``authorization.presentation.mode``
chooses whether technical details are added.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field

from custom_messages.contracts.authorization_presentation_mode import (
    AuthorizationPresentationMode,
    normalize_authorization_presentation_mode,
)


class AuthorizationErrorTechnicalDetailV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    operation: str = Field(min_length=1, max_length=500)
    status_code: int = Field(ge=400, le=599)
    failure_type: str = Field(min_length=1, max_length=240)


class AuthorizationErrorDetailV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,119}$")
    title: str = Field(min_length=1, max_length=240)
    message: str = Field(min_length=1, max_length=2000)
    recovery: str = Field(min_length=1, max_length=2000)
    technical: AuthorizationErrorTechnicalDetailV1 | None = None


class AuthorizationErrorResponseV1(BaseModel):
    """The JSON error body of a refused or failed browser action."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)
    schema_version: Literal[1] = 1
    error: AuthorizationErrorDetailV1


class _ErrorContext(TypedDict):
    presentation_mode: AuthorizationPresentationMode
    operation: str
    failure_type: str


@dataclass(frozen=True)
class AuthorizationErrorPresentation:
    """Stable user-facing error contract separated from internal exception text."""

    status_code: int
    code: str
    title: str
    message: str
    recovery: str
    presentation_mode: AuthorizationPresentationMode
    operation: str
    failure_type: str

    def payload(self) -> dict[str, object]:
        technical = None
        if self.presentation_mode == AuthorizationPresentationMode.TECHNICAL:
            technical = AuthorizationErrorTechnicalDetailV1(
                operation=self.operation,
                status_code=self.status_code,
                failure_type=self.failure_type,
            )
        return AuthorizationErrorResponseV1(
            error=AuthorizationErrorDetailV1(
                code=self.code,
                title=self.title,
                message=self.message,
                recovery=self.recovery,
                technical=technical,
            )
        ).model_dump(mode="json", exclude_none=True)

    def user_message(self) -> str:
        """Render channel-safe failure copy for the configured mode."""

        parts = [self.message, self.recovery]
        if self.presentation_mode == AuthorizationPresentationMode.TECHNICAL:
            parts.append(
                "Technical: "
                f"code={self.code}; status={self.status_code}; "
                f"operation={self.operation}; failure_type={self.failure_type}."
            )
        return " ".join(part for part in parts if part)


def present_authorization_error(
    error: Exception,
    *,
    operation: str,
    presentation_mode: AuthorizationPresentationMode | str = (
        AuthorizationPresentationMode.FRIENDLY
    ),
) -> AuthorizationErrorPresentation:
    """Translate a refused or failed control action without exposing internals."""

    mode = normalize_authorization_presentation_mode(presentation_mode)
    detail = str(error or "").strip().lower()
    operation_text = (
        str(operation or "complete this action").strip()[:500] or "complete this action"
    )
    common: _ErrorContext = {
        "presentation_mode": mode,
        "operation": operation_text,
        "failure_type": (type(error).__name__[:240] or "Exception"),
    }
    if any(phrase in detail for phrase in ("changed after", "changed before", "digest", "no longer pending")):
        return AuthorizationErrorPresentation(
            status_code=409,
            code="request_changed",
            title="Changed since you opened it",
            message=f"The system could not {operation_text} because this item changed after the page was loaded.",
            recovery="Refresh and review the latest version before trying again.",
            **common,
        )
    if "not found" in detail:
        return AuthorizationErrorPresentation(
            status_code=404,
            code="request_not_found",
            title="Not found",
            message=f"The system could not {operation_text} because the item no longer exists.",
            recovery="Refresh the list.",
            **common,
        )
    if isinstance(error, PermissionError):
        return AuthorizationErrorPresentation(
            status_code=403,
            code="access_denied",
            title="You cannot make this change",
            message=f"The system could not {operation_text} because your account cannot make this change.",
            recovery="Ask a team administrator to make it.",
            **common,
        )
    if any(phrase in detail for phrase in ("offline", "unavailable", "not configured", "disabled")):
        return AuthorizationErrorPresentation(
            status_code=503,
            code="authorization_service_unavailable",
            title="Service unavailable",
            message=f"The system could not {operation_text} because a supporting service is unavailable.",
            recovery="Wait a moment and try again. Contact an administrator if the problem continues.",
            **common,
        )
    if isinstance(error, RuntimeError):
        return AuthorizationErrorPresentation(
            status_code=502,
            code="authorization_action_failed",
            title="Action not completed",
            message=(
                f"The system could not confirm that it finished trying to "
                f"{operation_text} because a supporting service reported an error."
            ),
            recovery="Refresh the current state before trying again. Contact an administrator if the problem continues.",
            **common,
        )
    return AuthorizationErrorPresentation(
        status_code=400,
        code="invalid_authorization_request",
        title="Request could not be completed",
        message=f"The system could not {operation_text} because the submitted request was not valid.",
        recovery="Refresh, review the current choices, and try again.",
        **common,
    )
