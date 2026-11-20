"""The one-time claim every broker form submission goes through.

A form page (credential collection, account link, sign-in) is answered once.
Before a handler yields to broker work it claims the page: the claim fails when
the page was already answered, is being answered, or expired. Decline goes
through the same claim (secure_tool_broker.page_decline), so a page is either
answered or declined, never both. The claim is held until the worker that does
the broker work exits, even if the browser disconnects, and a handler that
records the worker's result afterwards (stored, deleted, signed in) records it
before the claim is released (``run_claimed_form_thread(on_result=...)``):
released first, a Decline could claim the page in between and be accepted
after the credential was already stored.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Callable

from aiohttp import web

from secure_tool_broker.account_links import STATUS_PENDING as ACCOUNT_LINK_PENDING, AccountLinkSession
from secure_tool_broker.auth_flows import BrokerAuthSession
from secure_tool_broker.redaction import redact_text
from secure_tools.broker_pages import SIGN_IN_STATUS_ERROR

LOGGER = logging.getLogger(__name__)
# A credential form whose result could not be recorded ends as unavailable.
_UNRECORDED_CREDENTIAL_STATUS = "credential_unavailable"


class FormSubmissionClaim(StrEnum):
    """Result of atomically claiming a short-lived broker form session."""

    ACCEPTED = "accepted"
    EXPIRED = "expired"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"


def claim_form_submission(
    session: Any,
    *,
    now_utc: datetime | None = None,
) -> FormSubmissionClaim:
    """Linearize form acceptance before a handler yields to broker work."""

    if session.used:
        return FormSubmissionClaim.RESOLVED
    if session.submission_in_progress:
        return FormSubmissionClaim.IN_PROGRESS
    now = now_utc or datetime.now(timezone.utc)
    if session.expires_at <= now:
        return FormSubmissionClaim.EXPIRED
    session.submission_in_progress = True
    return FormSubmissionClaim.ACCEPTED


def finish_form_submission(session: Any) -> None:
    session.submission_in_progress = False


async def run_claimed_form_thread(
    session: Any,
    callback: Callable[..., Any],
    *args: Any,
    on_result: Callable[[Any], None] | None = None,
    log_ref: str = "",
    **kwargs: Any,
) -> Any:
    """Keep the claim until the worker exits, even if the request is cancelled.

    ``on_result(result)`` records what the worker did on the page (used,
    status, result). It runs in the worker's done callback, then the claim is
    released in that same callback, so no other request can claim the page
    between the two; it also runs when the browser disconnected and the
    handler never resumes. ``log_ref`` names the page in the log line of a
    recording that failed (such as ``credential_collection abc123``).
    """

    worker = asyncio.create_task(asyncio.to_thread(callback, *args, **kwargs))

    def settle(done: asyncio.Task[Any]) -> None:
        try:
            if on_result is not None and not done.cancelled() and done.exception() is None:
                _record(session, on_result, done.result(), log_ref)
        finally:
            finish_form_submission(session)

    worker.add_done_callback(settle)
    return await asyncio.shield(worker)


def _record(session: Any, on_result: Callable[[Any], None], result: Any, log_ref: str) -> None:
    """Record a worker's result; a recording that fails still ends the page, so it is not answered twice."""

    try:
        on_result(result)
    except Exception:
        LOGGER.exception(
            "A broker form's result could not be recorded; the page is ended as unavailable. "
            "event=broker_form_result_record_failed page=%s",
            log_ref or type(session).__name__,
        )
        session.used = True
        if isinstance(session, BrokerAuthSession):
            session.state["status"] = SIGN_IN_STATUS_ERROR
        else:
            session.status = _UNRECORDED_CREDENTIAL_STATUS


def require_auth_form_available(session: BrokerAuthSession) -> None:
    if session.submission_in_progress:
        raise web.HTTPConflict(
            text="Auth session submission is already being processed."
        )


def claim_auth_form_submission(
    session: BrokerAuthSession,
    *,
    prune_expired: Callable[[], None],
) -> None:
    claim = claim_form_submission(session)
    if claim is FormSubmissionClaim.EXPIRED:
        prune_expired()
        raise web.HTTPGone(text="Auth session expired before submission.")
    if claim is not FormSubmissionClaim.ACCEPTED:
        raise web.HTTPConflict(
            text="Auth session submission is already being processed."
        )


def claim_account_link_submission(
    session: AccountLinkSession,
    *,
    prune_expired: Callable[[], None],
) -> None:
    if session.submission_in_progress:
        raise web.HTTPConflict(text="This account link form is already being processed.")
    if session.status != ACCOUNT_LINK_PENDING or session.used:
        raise web.HTTPConflict(text="This account link form was already used.")
    claim = claim_form_submission(session)
    if claim is FormSubmissionClaim.EXPIRED:
        prune_expired()
        raise web.HTTPGone(text="This account link form expired.")
    if claim is not FormSubmissionClaim.ACCEPTED:
        raise web.HTTPConflict(text="This account link form is already being processed.")


def require_credential_collection_submission_available(
    session: Any,
) -> None:
    if session.submission_in_progress:
        raise web.HTTPConflict(
            text="Credential collection submission is already being processed."
        )
    if session.status != "pending":
        raise web.HTTPGone(
            text=redact_text(
                str((session.result or {}).get("error") or "")
                or "Credential collection session is no longer available."
            )
        )


def claim_credential_collection_submission(
    session: Any,
    *,
    prune_expired: Callable[[], None],
) -> None:
    if session.status != "pending":
        raise web.HTTPGone(
            text="Credential collection session expired before submission."
        )
    claim = claim_form_submission(session)
    if claim is FormSubmissionClaim.EXPIRED:
        prune_expired()
        raise web.HTTPGone(text="Credential collection session expired.")
    if claim is not FormSubmissionClaim.ACCEPTED:
        raise web.HTTPConflict(
            text="Credential collection submission is already being processed."
        )
