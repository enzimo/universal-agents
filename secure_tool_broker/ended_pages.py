"""What a one-time sign-in page shows once it has ended.

A sign-in page (a token form, a browser sign-in, ``/auth model``, ``/upgrade``)
can be opened twice: in its Web Chat card and with "Open in its own tab", and
the card is rebuilt after a thread switch. Once the page ended in one view, the
other view must show how it ended, never a bare "no longer available" error: a
card shrinks to its one-line note only when the page it shows carries its
outcome (``auth_pages.render_broker_auth_page(card_outcome=...)``), and broker
cards have no Dismiss.

- ``ended_sign_in_page``: the page of a used sign-in session, for any later
  GET or POST. Nothing is applied.
- The token forms' result pages, used both for the submission's own answer and
  for every later view, so the two cannot differ.

Kept out of ``sign_in_pages`` and ``page_decline`` because it needs both:
``page_decline`` imports ``form_claims``, which imports ``auth_flows``, which
imports ``sign_in_pages``. Only ``service`` imports this module.
"""

from __future__ import annotations

import html
from typing import Any

from secure_tool_broker.auth_flows import BrokerAuthFlow, BrokerAuthSession
from secure_tool_broker.auth_pages import (
    CARD_OUTCOME_FINISHED,
    auth_display_name,
    render_broker_auth_page,
    render_close_page_button,
)
from secure_tool_broker.page_decline import declined_page
from secure_tools.auth_methods import GOOGLE_TEAM_CLIENT_METHOD, page_command, sign_in_command
from secure_tools.broker_pages import SIGN_IN_MAY_REMAIN_FIELD, SIGN_IN_STATUS_DECLINED, SIGN_IN_STATUS_SUCCESS

# What a Bitwarden "Save for Restart Unlock" submission (result action
# executors.BITWARDEN_RESTART_UNLOCK_ACTION) saved, kept in the session's state with the
# result for every later view: {"pending_file", "expires_at"}.
BITWARDEN_RESTART_UNLOCK_STATE = "restart_unlock_saved"


def token_sign_in_success_page(provider: str) -> str:
    """A token form (GitHub, Google token import, Bitwarden) that signed in."""

    display_name = auth_display_name(provider)
    body = (
        '<section class="content-panel">'
        f'<p class="eyebrow">{html.escape(display_name)}</p>'
        f"<h1>{html.escape(display_name)} authentication complete.</h1>"
        '<p class="lede">You can close this browser tab and retry the agent task.</p>'
        f"{render_close_page_button()}"
        "</section>"
    )
    return render_broker_auth_page(
        f"{display_name} authentication complete", body, card_outcome=CARD_OUTCOME_FINISHED
    )


def google_team_client_stored_page() -> str:
    """The team's Google sign-in app was stored."""

    body = (
        '<section class="content-panel">'
        '<p class="eyebrow">Google</p>'
        "<h1>The team's Google sign-in app is stored.</h1>"
        f'<p class="lede">People who have not signed in yet run <code>{sign_in_command("google")}</code>. '
        "A sign-in made through a different app must be made again.</p>"
        f"{render_close_page_button()}"
        "</section>"
    )
    return render_broker_auth_page("Google sign-in app stored", body, card_outcome=CARD_OUTCOME_FINISHED)


def bitwarden_restart_unlock_saved_page(pending_file: str, expires_at: str) -> str:
    """Bitwarden's unlock request was saved for the next broker start."""

    body = (
        '<section class="content-panel">'
        '<p class="eyebrow">Bitwarden</p>'
        "<h1>Bitwarden restart unlock saved.</h1>"
        '<p class="lede">Restart the broker or container now. On startup, the broker will complete Bitwarden login, '
        "verify the session, write <code>BW_SESSION_FILE</code>, and delete the pending request.</p>"
        f"<p>Pending file: <code>{html.escape(pending_file)}</code></p>"
        f"<p>Expires at: <code>{html.escape(expires_at)}</code></p>"
        "</section>"
    )
    # No outcome: the person still has to restart the broker, and only this page says so.
    return render_broker_auth_page("Bitwarden restart unlock saved", body)


def token_form_failed_page(provider: str, method: str) -> str:
    """A token form whose submission did not finish, seen again later.

    The submission's own answer showed why. This page says only what is known:
    the page is used up, and which command opens a new one (``page_command``:
    the team's sign-in app, a person's sign-in or ``/auth <provider>``). It
    does not say nothing was stored: a Bitwarden attempt may have logged in
    before its unlock failed, and a result that could not be recorded
    (``form_claims``) ends here too.
    """

    display_name = auth_display_name(provider)
    body = (
        '<section class="content-panel">'
        f'<p class="eyebrow">{html.escape(display_name)}</p>'
        "<h1>This page did not finish.</h1>"
        f"<p>It cannot be used again. Open a new one with <code>{html.escape(page_command(provider, method))}</code>.</p>"
        f"{render_close_page_button()}"
        "</section>"
    )
    # No outcome, like the failed submission's own page; the gateway reports the error.
    return render_broker_auth_page(f"{display_name} page did not finish", body)


def ended_sign_in_page(session: BrokerAuthSession, flow: BrokerAuthFlow | None) -> str:
    """The page of a sign-in session that has ended, as it ended."""

    state: dict[str, Any] = session.state
    if state.get("status") == SIGN_IN_STATUS_DECLINED:
        return declined_page(
            auth_display_name(session.provider), sign_in_may_remain=bool(state.get(SIGN_IN_MAY_REMAIN_FIELD))
        )
    if flow is not None:
        # /auth model and /upgrade draw their own closed page; browser sign-ins their ending.
        return flow.render(session)
    if state.get("status") != SIGN_IN_STATUS_SUCCESS:
        return token_form_failed_page(session.provider, session.method)
    if session.provider == "google" and session.method == GOOGLE_TEAM_CLIENT_METHOD:
        return google_team_client_stored_page()
    saved = state.get(BITWARDEN_RESTART_UNLOCK_STATE)
    if session.provider == "bitwarden" and isinstance(saved, dict):
        return bitwarden_restart_unlock_saved_page(str(saved.get("pending_file") or ""), str(saved.get("expires_at") or ""))
    return token_sign_in_success_page(session.provider)


__all__ = [
    "BITWARDEN_RESTART_UNLOCK_STATE",
    "bitwarden_restart_unlock_saved_page",
    "ended_sign_in_page",
    "google_team_client_stored_page",
    "token_form_failed_page",
    "token_sign_in_success_page",
]
