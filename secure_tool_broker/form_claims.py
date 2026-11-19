"""The one-time claim every broker form submission goes through.

A form page (credential collection, account link, sign-in) is answered once.
Before a handler yields to broker work it claims the page: the claim fails when
the page was already answered, is being answered, or expired. Decline goes
through the same claim (secure_tool_broker.page_decline), so a page is either
answered or declined, never both. The claim is held until the worker that does
the broker work exits, even if the browser disconnects.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Callable

from aiohttp import web

from secure_tool_broker.account_links import STATUS_PENDING as ACCOUNT_LINK_PENDING, AccountLinkSession
from secure_tool_broker.auth_flows import BrokerAuthSession
from secure_tool_broker.redaction import redact_text


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
    **kwargs: Any,
) -> Any:
    """Keep the claim until the worker exits, even if the request is cancelled."""

    worker = asyncio.create_task(asyncio.to_thread(callback, *args, **kwargs))
    worker.add_done_callback(lambda _worker: finish_form_submission(session))
    return await asyncio.shield(worker)


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
