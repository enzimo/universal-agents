"""Decline on broker pages: ending a page the person does not want to answer.

Every open broker form a person or task waits on carries a Decline button
(``auth_pages.render_broker_auth_page(decline=True)``): a form of its own with
only ``defaults.DECLINE_FIELD``, posted to the page's own address. The page's
POST handler (``secure_tool_broker.service``) sends it here.

- A credential collection or deletion form ends as ``credential_declined``:
  nothing is stored or deleted, and the gateway tells the waiting task.
- A sign-in page ends as ``declined`` and its sign-in process (gh, gog) stops.
  The process monitor (``auth_flows._monitor_web_auth_process``) then leaves the
  page declined and stores nothing.
- Account links decline through their own existing path (``finish_account_link``
  without confirmation).

A Decline goes through the same one-time claim as a submission
(``form_claims``), so a page is either answered or declined, never both. A
sign-in page that already ended, whose process already finished signing in, or
that ends with its own Close (``/auth model``, ``/upgrade``) is not declined:
it is answered as it stands (``decline_refusal``), because the card and its own
tab can both show a Decline that is no longer current. A page already used
(answered, declined or closed) never gets here: the service answers any form
sent to it with its ending (``ended_pages.ended_sign_in_page``).
"""

from __future__ import annotations

import html
import logging
from typing import Any, Callable

from aiohttp import web

from custom_messages.contracts.credentials_v1 import CREDENTIAL_DECLINED
from secure_tool_broker.auth_pages import (
    CARD_OUTCOME_DECLINED,
    CARD_OUTCOME_FINISHED,
    auth_display_name,
    render_broker_auth_page,
    render_close_page_button,
)
from secure_tool_broker.credential_state import credential_owner_context
from secure_tool_broker.form_claims import (
    claim_auth_form_submission,
    claim_credential_collection_submission,
    finish_form_submission,
    run_claimed_form_thread,
)
from secure_tools.auth_methods import SIGN_IN_STATUS_COMMAND
from secure_tools.broker_pages import SIGN_IN_FINAL_STATUSES, SIGN_IN_MAY_REMAIN_FIELD, SIGN_IN_STATUS_DECLINED

# Why a Decline was not applied to a sign-in page (event=broker_auth_decline_ignored reason=...).
REFUSED_PAGE_WITHOUT_DECLINE = "page_without_decline"
REFUSED_PAGE_ALREADY_ENDED = "page_already_ended"
REFUSED_SIGN_IN_ALREADY_FINISHED = "sign_in_already_finished"


def declined_page(eyebrow: str, *, sign_in_may_remain: bool = False) -> str:
    """The page a person declined: closed, nothing saved; a card tells the chat it was declined.

    The words hold whether a task waits on the page or the person opened it with a command.
    With ``sign_in_may_remain`` (a sign-in's CLI finished signing in just before
    Decline stopped it, ``auth_flows.sign_in_may_remain_after_decline``) the page
    does not say nothing was saved, and the card reads "Done." (finished), which
    claims nothing about saving, instead of "Declined. Nothing was saved."
    """

    if sign_in_may_remain:
        saved = (
            f"<p>{html.escape(eyebrow)} may have finished signing in just before you declined, so a sign-in may "
            f"remain. Check with {html.escape(SIGN_IN_STATUS_COMMAND)}. The page is closed. If an agent asked for it, "
            "it is told you declined.</p>"
        )
        outcome = CARD_OUTCOME_FINISHED
    else:
        saved = "<p>Nothing was saved, and the page is closed. If an agent asked for it, it is told.</p>"
        outcome = CARD_OUTCOME_DECLINED
    body = (
        f'<section class="content-panel"><p class="eyebrow">{html.escape(eyebrow)}</p>'
        f"<h1>You declined this page.</h1>{saved}"
        f"{render_close_page_button()}</section>"
    )
    return render_broker_auth_page("Declined", body, card_outcome=outcome)


def decline_credential_collection(
    session_id: str, session: Any, *, prune_expired: Callable[[], None], logger: logging.Logger
) -> None:
    """End a credential collection or deletion form as declined: nothing stored or deleted, the task is told."""

    claim_credential_collection_submission(session, prune_expired=prune_expired)
    try:
        session.status = CREDENTIAL_DECLINED
        session.result = {
            "status": "declined",
            "content": {
                "status": CREDENTIAL_DECLINED,
                "message": "The person declined the form; nothing was stored or deleted.",
            },
        }
        session.used = True
    finally:
        finish_form_submission(session)
    logger.info(
        "A person declined a credential form. event=credential_collection_declined credential_key=%s "
        "flow=%s session_id_prefix=%s",
        session.request_payload.get("credential_key"),
        session.request_payload.get("flow") or "collect",
        session_id[:12],
    )


def decline_refusal(session: Any, flow: Any) -> str | None:
    """Why this sign-in page cannot be declined now, or None while it is open and offers Decline.

    A page whose sign-in already ended (success, error or declined) keeps that
    ending: a Decline from a stale second view must not turn a stored sign-in
    into "declined". The long-lived pages have their own Close
    (``declinable = False``). A sign-in process that already exited 0 has
    finished signing in; its real result is reported instead.
    """

    if flow is not None and not getattr(flow, "declinable", True):
        return REFUSED_PAGE_WITHOUT_DECLINE
    if str(session.state.get("status") or "") in SIGN_IN_FINAL_STATUSES:
        return REFUSED_PAGE_ALREADY_ENDED
    process = session.process
    if process is not None and process.poll() == 0:
        return REFUSED_SIGN_IN_ALREADY_FINISHED
    return None


async def decline_auth_session(
    session_id: str, session: Any, flow: Any, *, prune_expired: Callable[[], None], logger: logging.Logger
) -> web.Response:
    """End a sign-in page as declined and stop its sign-in process; or answer with the page as it stands.

    "declined" is set before the process is stopped, so the process monitor
    leaves it and stores nothing.
    """

    claim_auth_form_submission(session, prune_expired=prune_expired)
    refusal = decline_refusal(session, flow)
    if refusal is not None:
        finish_form_submission(session)
        logger.info(
            "A Decline was not applied to a sign-in page. event=broker_auth_decline_ignored provider=%s method=%s "
            "reason=%s session_id_prefix=%s",
            session.provider,
            session.method,
            refusal,
            session_id[:12],
        )
        if flow is None:
            raise web.HTTPConflict(text="This page can no longer be declined.")
        return web.Response(text=flow.render(session), content_type="text/html")
    session.state["status"] = SIGN_IN_STATUS_DECLINED
    session.state["message"] = "The person declined this page."
    session.used = True
    if flow is None:
        finish_form_submission(session)
    else:
        # Stopping a sign-in process can wait a few seconds; the claim is released when it is done.
        await run_claimed_form_thread(session, _cleanup_as_owner, session, flow)
    logger.info(
        "A person declined a sign-in page. event=broker_auth_session_declined provider=%s method=%s "
        "session_id_prefix=%s",
        session.provider,
        session.method,
        session_id[:12],
    )
    return web.Response(
        text=declined_page(
            auth_display_name(session.provider), sign_in_may_remain=bool(session.state.get(SIGN_IN_MAY_REMAIN_FIELD))
        ),
        content_type="text/html",
    )


def _cleanup_as_owner(session: Any, flow: Any) -> None:
    with credential_owner_context(session.owner_canonical_user_id):
        flow.cleanup(session)


__all__ = [
    "REFUSED_PAGE_ALREADY_ENDED",
    "REFUSED_PAGE_WITHOUT_DECLINE",
    "REFUSED_SIGN_IN_ALREADY_FINISHED",
    "decline_auth_session",
    "decline_credential_collection",
    "decline_refusal",
    "declined_page",
]
