"""Open the secure broker's account link form for one person.

An account link says which stored credential the secure tool broker uses for
one catalog tool (``http:portainer``, ``cli:x``, ``mcp:<connection>``), either
for the whole team or for one person. A link grants nothing: access to the
tool still comes from grants.

The gateway vouches for the person and their current team roles (only the
gateway's broker token may open these forms). The broker decides whether the
person may link for the requested owner (a team link needs the ``admin`` or
``owner`` role), renders the labeled form, and stores the link after the
person picks one of their stored credentials. Credential values never pass
through the gateway.

Three callers open forms through ``GatewayAccountLinks``, and the gateway
watches every form until it is used or expires
(``broker_session_watch``):

- an agent task that sends a ``ToolSetupRequestV1`` of kind
  ``account_link``; ``tool_setup`` shows the form to the person the task
  works for, waits for the result and reports it back to the task;
- an approved access proposal with account links; ``tool_setup`` opens the
  forms for the approver and the proposal's task resumes once they end;
- a person's ``/link <tool_id> [team|me]`` chat command; ``link_results``
  posts how the form ended in the chat where they ran it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import ValidationError

from assistant_gateway.application.broker_sessions import (
    validated_broker_form_session,
    validated_user_form_url,
)
from assistant_gateway.application.ports import (
    GatewayClock,
    GatewaySecureBroker,
    GatewaySecureBrokerRefusal,
)
from custom_messages.contracts.access_v2 import AccountLinkRequestV1, LinkOwnerKind


AccountLinkOwner = Literal["team", "person"]

ACCOUNT_LINK_OWNER_TEAM: AccountLinkOwner = "team"
ACCOUNT_LINK_OWNER_PERSON: AccountLinkOwner = "person"

ACCOUNT_LINK_STATUS_CREATED = "account_link_created"
ACCOUNT_LINK_STATUS_DECLINED = "account_link_declined"
ACCOUNT_LINK_STATUS_UNAVAILABLE = "account_link_unavailable"
ACCOUNT_LINK_TERMINAL_STATUSES = frozenset(
    {
        ACCOUNT_LINK_STATUS_CREATED,
        ACCOUNT_LINK_STATUS_DECLINED,
        ACCOUNT_LINK_STATUS_UNAVAILABLE,
    }
)

# The broker's reason when a form's time ran out before anyone used it.
REASON_SESSION_EXPIRED = "account_link_session_expired"

# Gateway-side reasons a form could not open; broker refusals keep the
# broker's own reason code (for example account_link_team_admin_required).
REASON_BROKER_UNAVAILABLE = "account_link_broker_unavailable"
REASON_INVALID_REQUEST = "account_link_invalid_request"
REASON_BROKER_ERROR = "account_link_broker_error"
REASON_SESSION_INVALID = "account_link_session_invalid"
REASON_TEAM_ADMIN_REQUIRED = "account_link_team_admin_required"

ACCOUNT_LINK_NO_ACCESS_NOTE = "Linking an account gives nobody access by itself."
ACCOUNT_LINK_FORM_ACTION_LABEL = "Open account link form"


def account_link_owner_label(owner: str) -> str:
    """Name whose account the link is for, from the person's point of view."""

    return "the team's account" if owner == ACCOUNT_LINK_OWNER_TEAM else "your own account"


def account_link_status_is_final(payload: dict[str, Any]) -> bool:
    """Whether the broker's form status says the form is finished."""

    return str(payload.get("status") or "") in ACCOUNT_LINK_TERMINAL_STATUSES


@dataclass(frozen=True)
class AccountLinkForm:
    """One open account link form; ``form_url`` is the browser-facing URL.

    ``tool_display_name`` is the tool's name from the broker's catalog
    (``Crawl4AI`` for ``mcp:crawl4ai``), used in messages to the person.
    """

    session_id: str
    form_url: str
    status_url: str
    expires_at_utc: str
    tool_display_name: str


class AccountLinkUnavailable(Exception):
    """The form could not be opened; ``message`` says why in plain language."""

    def __init__(self, message: str, *, reason_code: str) -> None:
        super().__init__(message)
        self.message = message
        self.reason_code = reason_code


def _invalid_request_message(error: ValueError, *, tool_id: str) -> str:
    fields = (
        {str(item["loc"][0]) for item in error.errors() if item.get("loc")}
        if isinstance(error, ValidationError)
        else {"owner_kind"}
    )
    if "tool_id" in fields:
        return (
            f"{tool_id} is not a tool id. Tool ids look like http:portainer, "
            "cli:x or mcp:<connection>."
        )
    return f"The account link request is not valid ({', '.join(sorted(fields))})."


class GatewayAccountLinks:
    """Open account link forms through the secure broker."""

    def __init__(
        self,
        *,
        broker: GatewaySecureBroker,
        team_id: str,
        clock: GatewayClock,
        logger: logging.Logger | None = None,
    ) -> None:
        self._broker = broker
        self._team_id = team_id
        self._clock = clock
        self._logger = logger or logging.getLogger(__name__)

    async def open_form(
        self,
        *,
        request_id: str,
        person_id: str,
        person_roles: tuple[str, ...],
        tool_id: str,
        owner: AccountLinkOwner,
        reason: str,
    ) -> AccountLinkForm:
        """Open the form for ``person_id``; raise ``AccountLinkUnavailable`` if refused."""

        if not self._broker.available:
            detail = self._broker.auth_unavailable_message or (
                "The secure tool broker is not set up for this gateway."
            )
            raise AccountLinkUnavailable(
                f"Account link forms are unavailable. {detail}",
                reason_code=REASON_BROKER_UNAVAILABLE,
            )
        try:
            request = AccountLinkRequestV1(
                request_id=request_id,
                team_id=self._team_id,
                tool_id=tool_id,
                owner_kind=LinkOwnerKind(owner),
                person_id=person_id,
                person_roles=person_roles,
                reason=reason,
            )
        except (ValidationError, ValueError) as exc:
            self._logger.warning(
                "Account link request was not valid. "
                "event=gateway_account_link_request_invalid request_id=%s tool_id=%s "
                "owner=%s error=%s",
                request_id,
                tool_id,
                owner,
                exc,
            )
            raise AccountLinkUnavailable(
                _invalid_request_message(exc, tool_id=tool_id),
                reason_code=REASON_INVALID_REQUEST,
            ) from exc
        payload = await self._create_session(request)
        try:
            session = validated_broker_form_session(payload, now=self._clock.now_utc())
            form_url = validated_user_form_url(
                self._broker.user_form_url(session.form_url)
            )
            tool_display_name = str(payload.get("tool_display_name") or "").strip()
            if not tool_display_name:
                raise ValueError("missing required field: tool_display_name")
        except ValueError as exc:
            self._logger.error(
                "Broker account link session response was invalid. "
                "event=gateway_account_link_session_invalid request_id=%s tool_id=%s "
                "error=%s",
                request_id,
                tool_id,
                exc,
            )
            raise AccountLinkUnavailable(
                "The secure tool broker returned an unusable form. Try again later.",
                reason_code=REASON_SESSION_INVALID,
            ) from exc
        self._logger.info(
            "Account link form opened. event=gateway_account_link_form_opened "
            "request_id=%s tool_id=%s owner=%s person_id=%s session_id_prefix=%s",
            request_id,
            tool_id,
            owner,
            person_id,
            session.session_id[:12],
        )
        return AccountLinkForm(
            session_id=session.session_id,
            form_url=form_url,
            status_url=session.status_url,
            expires_at_utc=session.expires_at,
            tool_display_name=tool_display_name,
        )

    async def _create_session(self, request: AccountLinkRequestV1) -> dict[str, Any]:
        try:
            return await self._broker.create_account_link_session(request)
        except GatewaySecureBrokerRefusal as exc:
            self._logger.warning(
                "Broker refused the account link form. "
                "event=gateway_account_link_refused request_id=%s tool_id=%s "
                "owner_kind=%s person_id=%s status=%s reason_code=%s",
                request.request_id,
                request.tool_id,
                request.owner_kind.value,
                request.person_id,
                exc.status,
                exc.reason_code or "-",
            )
            raise AccountLinkUnavailable(
                exc.message or "The secure tool broker refused the account link form.",
                reason_code=exc.reason_code or REASON_BROKER_ERROR,
            ) from exc
        except Exception as exc:
            self._logger.warning(
                "Broker account link form could not be opened. "
                "event=gateway_account_link_create_failed request_id=%s tool_id=%s "
                "error=%s",
                request.request_id,
                request.tool_id,
                exc,
            )
            raise AccountLinkUnavailable(
                "The secure tool broker did not answer, so the form could not be "
                "opened. Try again later.",
                reason_code=REASON_BROKER_ERROR,
            ) from exc


__all__ = [
    "ACCOUNT_LINK_FORM_ACTION_LABEL",
    "ACCOUNT_LINK_NO_ACCESS_NOTE",
    "ACCOUNT_LINK_OWNER_PERSON",
    "ACCOUNT_LINK_OWNER_TEAM",
    "ACCOUNT_LINK_STATUS_CREATED",
    "ACCOUNT_LINK_STATUS_DECLINED",
    "ACCOUNT_LINK_STATUS_UNAVAILABLE",
    "ACCOUNT_LINK_TERMINAL_STATUSES",
    "AccountLinkForm",
    "AccountLinkOwner",
    "AccountLinkUnavailable",
    "GatewayAccountLinks",
    "REASON_SESSION_EXPIRED",
    "REASON_TEAM_ADMIN_REQUIRED",
    "account_link_owner_label",
    "account_link_status_is_final",
]
