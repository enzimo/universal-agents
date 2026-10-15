"""A person's own X sign-in for the X tool (``cli:x``), in the browser.

Why this module exists: X (run through ``xurl`` by ``xurl_executor``) used to
need a pasted OAuth JSON document that someone produced by hand. Now each
person signs in with their own X account through X's OAuth 2.0 authorization
code flow with PKCE (https://docs.x.com/resources/fundamentals/authentication/oauth-2-0/authorization-code):

1. A team administrator stores the team's X app once (``/auth x
   team_client``): its client id, its client secret when X issued one (a
   "Web App"; a "Native App" has none), and the callback address registered
   in the X developer portal. It is kept in the team's part of the credential
   store; it gives nobody access to anyone's X account.
2. ``/link cli:x`` opens this sign-in page for the person. The broker makes a
   one-time ``state`` and PKCE verifier, and the person approves on X.
3. X sends the browser to the callback address with ``code`` and ``state``.
   If that address is the broker's own ``/auth/x/callback`` the broker finishes
   by itself; otherwise (``http://localhost:8080/callback``, which does not
   load) the person pastes the address into the page.
4. The broker exchanges the code (with the verifier) for tokens, asks X for
   the account's username, and stores the result under the person
   (``x.sign_in_json``) in the same shape the X executor already reads
   (``xurl_executor.OAuthCredentials``). The executor uses it for work done for
   that person and saves every refresh, the team's account link being the
   fallback for people who have not signed in.

Token values never reach logs, pages or the gateway.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from pydantic import ValidationError

from access_control.people import is_administrator
from secure_tool_broker.auth_models import AuthStatus
from secure_tool_broker.auth_pages import (
    SECRET_INPUT_TOGGLE_SCRIPT,
    render_broker_auth_page,
    render_close_page_button,
    render_credential_input,
    render_markdown_text,
    render_session_countdown,
)
from secure_tool_broker.credential_state import credential_owner_context
from secure_tool_broker.credential_state_store import read_credential_state, write_credential_state
from secure_tool_broker.xurl_executor import OAuthCredentials
from secure_tool_broker.sign_in_pages import auth_link_html, session_status_payload, sign_in_wait_script
from secure_tools.auth_methods import SIGN_IN_TOOLS, TEAM_CLIENT_METHOD, X_SIGN_IN_PROVIDER, team_client_command
from secure_tools.providers import ToolResult

LOGGER = logging.getLogger(__name__)

X_TOOL = SIGN_IN_TOOLS["cli:x"]
SIGN_IN_COMMAND = X_TOOL.command
TEAM_CLIENT_COMMAND = team_client_command(X_SIGN_IN_PROVIDER)
# Credential-state keys (credential_state_store.STATE_KEY_PATHS).
X_SIGN_IN_KEY = "x.sign_in_json"  # the person's sign-in, OAuthCredentials JSON
X_CLIENT_KEY = "x.oauth_client_json"  # the team's X app
# Names the refresh state of a person's sign-in in xurl's saved sessions
# (an account link uses its credential key there instead).
X_SIGN_IN_SESSION_KEY = "x-sign-in"

# X's OAuth 2.0 endpoints (the defaults xurl uses too); tests point them at a fake server.
AUTHORIZE_URL = "https://x.com/i/oauth2/authorize"
TOKEN_URL = "https://api.x.com/2/oauth2/token"
API_BASE_URL = "https://api.x.com"
# What the X tool does (read the account, publish posts) and a refresh token (offline.access).
SCOPES = ("tweet.read", "tweet.write", "users.read", "offline.access")
CALLBACK_PATH = "/auth/x/callback"
HTTP_TIMEOUT_SECONDS = 15
_MAX_CLIENT_FIELD = 4096

STATUS_CREATED = "created"
STATUS_PENDING = "pending"
STATUS_SUCCESS = "success"
STATUS_ERROR = "error"

CLIENT_MISSING_MESSAGE = (
    "No X sign-in app is set up for this team yet. A team administrator stores it once with "
    f"{TEAM_CLIENT_COMMAND}; then sign in again with {SIGN_IN_COMMAND}."
)
SIGN_IN_REQUIRED_MESSAGE = (
    f"You have not signed in to X for the agents yet. Sign in with {SIGN_IN_COMMAND}; "
    "the broker then uses your own X account for your requests only."
)
TEAM_CLIENT_ADMIN_MESSAGE = "Only a team administrator or owner can set the team's X sign-in app."
RESTART_TEXT = f"Start again with {SIGN_IN_COMMAND}."


class XSignInError(Exception):
    """A sign-in step failed; the message says why in plain language."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class XClient:
    """The team's X app: what people sign in through."""

    client_id: str
    redirect_uri: str
    client_secret: str = field(default="", repr=False)

    def as_json(self) -> str:
        return json.dumps(
            {"client_id": self.client_id, "client_secret": self.client_secret, "redirect_uri": self.redirect_uri}
        )


# -- The team's X app ------------------------------------------------------------------


def redirect_uri_problem(redirect_uri: str) -> str | None:
    """Why ``redirect_uri`` cannot be the app's callback address, or None."""

    parsed = urllib_parse.urlparse(redirect_uri)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        return "The callback address must be a full web address, such as https://broker.example.com/auth/x/callback."
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1"}:
        return "The callback address must use https, except on localhost or 127.0.0.1."
    if parsed.fragment:
        return "The callback address cannot contain a # part."
    return None


def client_problem(client_id: str, client_secret: str, redirect_uri: str) -> str | None:
    """Why these values are not a usable X app, or None."""

    if not client_id:
        return "Type the app's OAuth 2.0 client ID from the X developer portal."
    if max(len(client_id), len(client_secret), len(redirect_uri)) > _MAX_CLIENT_FIELD:
        return "One of the values is too long to be an X app setting."
    return redirect_uri_problem(redirect_uri)


def _parse_client(text: str) -> XClient | None:
    try:
        data = json.loads(text)
        client = XClient(
            client_id=str(data["client_id"]),
            client_secret=str(data.get("client_secret") or ""),
            redirect_uri=str(data["redirect_uri"]),
        )
    except (ValueError, KeyError, TypeError):
        return None
    return None if client_problem(client.client_id, client.client_secret, client.redirect_uri) else client


def team_client(team_id: str | None) -> XClient | None:
    """The team's stored X app, or None; a credential store failure raises ``XSignInError``."""

    if not team_id:
        return None
    with credential_owner_context(team_id, scope_kind="team"):
        stored = read_credential_state(X_CLIENT_KEY)
    if isinstance(stored, ToolResult):
        raise XSignInError(
            f"The broker could not read the team's X sign-in app. {stored.error or ''}".strip(),
            reason="x_client_unreadable",
        )
    if not stored:
        return None
    client = _parse_client(str(stored))
    if client is None:
        LOGGER.error("The team's stored X sign-in app is not usable. event=x_team_client_invalid team_id=%s", team_id)
        raise XSignInError(
            f"The team's stored X sign-in app is not usable. A team administrator stores it again with {TEAM_CLIENT_COMMAND}.",
            reason="x_client_invalid",
        )
    return client


def store_team_client(team_id: str, client: XClient, *, stored_by: str) -> ToolResult:
    """Store the team's X app; the caller has checked administrator authority."""

    problem = client_problem(client.client_id, client.client_secret, client.redirect_uri)
    if problem:
        return ToolResult(status="error", error=problem, content={"error_code": "x_client_invalid"})
    with credential_owner_context(team_id, scope_kind="team"):
        result = write_credential_state(X_CLIENT_KEY, client.as_json())
    if result.status == "success":
        LOGGER.info(
            "The team's X sign-in app was stored. event=x_team_client_stored team_id=%s person_id=%s confidential=%s",
            team_id,
            stored_by,
            bool(client.client_secret),
        )
    else:
        LOGGER.warning(
            "The team's X sign-in app could not be stored. event=x_team_client_store_failed team_id=%s person_id=%s",
            team_id,
            stored_by,
        )
    return result


# -- A person's sign-in ------------------------------------------------------------------


def person_sign_in(person_id: str) -> str | ToolResult | None:
    """The person's stored X sign-in (OAuthCredentials JSON), None, or the store's failure."""

    with credential_owner_context(person_id):
        stored = read_credential_state(X_SIGN_IN_KEY)
    if isinstance(stored, ToolResult):
        return stored
    return str(stored).strip() or None if stored else None


def pkce_pair() -> tuple[str, str]:
    """A PKCE verifier and its S256 challenge (RFC 7636)."""

    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def authorization_url(client: XClient, state: str, challenge: str) -> str:
    query = urllib_parse.urlencode(
        {
            "response_type": "code",
            "client_id": client.client_id,
            "redirect_uri": client.redirect_uri,
            "scope": " ".join(SCOPES),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
        quote_via=urllib_parse.quote,
    )
    return f"{AUTHORIZE_URL}?{query}"


def _x_error(exc: urllib_error.HTTPError) -> str:
    """X's own error words (never token values: X does not echo them)."""

    try:
        body = json.loads(exc.read().decode("utf-8", "replace"))
    except (ValueError, OSError):
        return f"HTTP {exc.code}"
    if not isinstance(body, dict):
        return f"HTTP {exc.code}"
    words = [str(body.get(key) or "") for key in ("error", "error_description", "title", "detail")]
    return ": ".join(word for word in words if word)[:300] or f"HTTP {exc.code}"


def _call_x(request: urllib_request.Request, *, step: str) -> dict[str, Any]:
    try:
        with urllib_request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:  # noqa: S310 - fixed X endpoints
            payload = json.loads(response.read().decode("utf-8"))
    except urllib_error.HTTPError as exc:
        detail = _x_error(exc)
        LOGGER.warning("X refused a sign-in step. event=x_sign_in_refused step=%s status=%s", step, exc.code)
        raise XSignInError(f"X refused the sign-in ({detail}).", reason=f"x_{step}_refused") from None
    except (urllib_error.URLError, TimeoutError, OSError, ValueError) as exc:
        LOGGER.warning(
            "X could not be reached for a sign-in step. event=x_sign_in_unreachable step=%s error_type=%s",
            step,
            type(exc).__name__,
        )
        raise XSignInError("The broker could not reach X to finish the sign-in. Try again later.", reason="x_unreachable") from None
    if not isinstance(payload, dict):
        raise XSignInError("X sent an answer the broker does not understand.", reason=f"x_{step}_invalid")
    return payload


def exchange_code(client: XClient, code: str, verifier: str) -> dict[str, Any]:
    """Trade the authorization code for tokens; a confidential app authenticates with HTTP Basic."""

    body = urllib_parse.urlencode(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": client.redirect_uri,
            "code_verifier": verifier,
            "client_id": client.client_id,
        }
    ).encode("ascii")
    headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
    if client.client_secret:
        pair = f"{urllib_parse.quote(client.client_id)}:{urllib_parse.quote(client.client_secret)}"
        headers["Authorization"] = "Basic " + base64.b64encode(pair.encode("utf-8")).decode("ascii")
    token = _call_x(urllib_request.Request(TOKEN_URL, data=body, headers=headers, method="POST"), step="token")
    if not token.get("access_token") or not token.get("refresh_token"):
        raise XSignInError(
            "X did not return a lasting sign-in (no refresh token). Check that the X app allows OAuth 2.0, then sign in again.",
            reason="x_refresh_token_missing",
        )
    return token


def account_username(access_token: str) -> str:
    """The signed-in account's username (``GET /2/users/me``)."""

    request = urllib_request.Request(
        f"{API_BASE_URL}/2/users/me",
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
    )
    data = _call_x(request, step="account").get("data")
    username = data.get("username") if isinstance(data, dict) else None
    if not isinstance(username, str) or not username:
        raise XSignInError("X did not say which account signed in.", reason="x_account_invalid")
    return username


def sign_in_record(client: XClient, token: Mapping[str, Any], username: str, *, now: float) -> str:
    """The stored sign-in, in the shape the X executor reads (``OAuthCredentials``)."""

    try:
        expires_in = max(0, int(token.get("expires_in") or 0))
        record = OAuthCredentials(
            client_id=client.client_id,
            client_secret=client.client_secret,
            username=username,
            access_token=str(token["access_token"]),
            refresh_token=str(token["refresh_token"]),
            expiration_time=int(now) + expires_in,
        )
    except (ValidationError, ValueError, TypeError, KeyError):
        raise XSignInError("X returned a sign-in the broker cannot use.", reason="x_token_invalid") from None
    return record.model_dump_json()


def store_person_sign_in(person_id: str, record: str) -> ToolResult:
    with credential_owner_context(person_id):
        return write_credential_state(X_SIGN_IN_KEY, record)


def x_auth_status(person_id: str | None, team_id: str | None) -> AuthStatus:
    """Whether the person signed in to X (the broker checks it with X when an agent uses it)."""

    def status(state: str, message: str, account: str | None = None) -> AuthStatus:
        # ``details.account`` is the account the person signed in with, for set_up_tools.
        return AuthStatus(provider=X_SIGN_IN_PROVIDER, status=state, authenticated=False, message=message,
                          auth_options=(SIGN_IN_COMMAND,), details={"account": account} if account else None)

    if not person_id:
        return status("auth_required", SIGN_IN_REQUIRED_MESSAGE)
    stored = person_sign_in(person_id)
    if isinstance(stored, ToolResult):
        return status("unavailable", f"The broker could not read your X sign-in. {stored.error or ''}".strip())
    if stored:
        try:
            username = OAuthCredentials.model_validate_json(stored).username
        except ValidationError:
            return status("auth_required", f"Your stored X sign-in is not usable. Sign in again with {SIGN_IN_COMMAND}.")
        return status(
            "signed_in", f"You signed in to X as @{username}. X checks it when an agent uses it.", account=username
        )
    try:
        has_client = team_client(team_id) is not None
    except XSignInError as exc:
        return status("unavailable", str(exc))
    return status("auth_required", SIGN_IN_REQUIRED_MESSAGE if has_client else CLIENT_MISSING_MESSAGE)


# -- The sign-in page (auth_flows registry) ------------------------------------------------


def _callback_params(address: str) -> dict[str, str]:
    """``code``, ``state`` and ``error`` from the address X sent the browser to."""

    query = urllib_parse.urlparse(address.strip()).query
    values = urllib_parse.parse_qs(query)
    return {key: values[key][0] for key in ("code", "state", "error", "error_description") if values.get(key)}


def _page(heading: str, body: str, *, script: str = "") -> str:
    return render_broker_auth_page(
        "X sign-in", f'<section class="content-panel"><p class="eyebrow">X</p><h1>{heading}</h1>{body}</section>', script=script
    )


class XWebSignInFlow:
    """``/link cli:x``: the person's own X sign-in with PKCE."""

    provider = X_SIGN_IN_PROVIDER
    method = "web"
    display_name = "X"
    kind = "browser_redirect_form"

    def start(self, session: Any, payload: Mapping[str, Any], logger: logging.Logger) -> None:
        try:
            client = team_client(session.owner_team_id)
        except XSignInError as exc:
            session.state.update({"status": STATUS_ERROR, "error": str(exc), "failure_reason": exc.reason})
            return
        if client is None:
            session.state.update(
                {"status": STATUS_ERROR, "error": CLIENT_MISSING_MESSAGE, "failure_reason": "x_client_missing"}
            )
            return
        verifier, challenge = pkce_pair()
        state = secrets.token_urlsafe(32)
        session.state.update(
            {
                "status": STATUS_PENDING,
                "oauth_state": state,
                "code_verifier": verifier,
                "client": client,
                "external_url": authorization_url(client, state, challenge),
                "message": "Waiting for you to approve the sign-in on X.",
            }
        )
        logger.info(
            "X sign-in started. event=x_sign_in_started person_id=%s confidential_app=%s",
            session.owner_canonical_user_id,
            bool(client.client_secret),
        )

    def matches(self, session: Any, state: str) -> bool:
        """Whether ``state`` is this session's one-time state (constant-time)."""

        expected = str(session.state.get("oauth_state") or "")
        return bool(expected and state) and hmac.compare_digest(expected, state)

    def submit(self, session: Any, form: Mapping[str, Any], logger: logging.Logger) -> None:
        """The person pasted the address X sent the browser to."""

        address = str(form.get("redirect_url") or "").strip()
        if not address:
            session.state["error"] = "Paste the full address X sent your browser to."
            return
        self.complete(session, _callback_params(address), logger)

    def complete(self, session: Any, params: Mapping[str, str], logger: logging.Logger) -> None:
        """Finish from the callback's ``code`` and ``state`` (callback route or pasted address)."""

        if session.state.get("status") != STATUS_PENDING:
            return
        if not self.matches(session, str(params.get("state") or "")):
            session.state["error"] = (
                "That address does not belong to this sign-in. Paste the address from the X sign-in you opened here."
            )
            return
        if params.get("error"):
            self._fail(session, logger, XSignInError(
                f"X did not approve the sign-in ({params['error']}). {RESTART_TEXT}", reason="x_not_approved"
            ))
            return
        try:
            username = self._finish(session, str(params.get("code") or ""))
        except XSignInError as exc:
            self._fail(session, logger, exc)
            return
        for key in ("oauth_state", "code_verifier", "client", "error"):
            session.state.pop(key, None)
        session.state.update(
            {"status": STATUS_SUCCESS, "username": username, "message": f"Signed in to X as @{username}."}
        )
        logger.info("A person's X sign-in was stored. event=x_sign_in_stored person_id=%s", session.owner_canonical_user_id)

    @staticmethod
    def _finish(session: Any, code: str) -> str:
        if not code:
            raise XSignInError(f"The address has no sign-in code. {RESTART_TEXT}", reason="x_code_missing")
        client: XClient = session.state["client"]
        token = exchange_code(client, code, str(session.state["code_verifier"]))
        username = account_username(str(token["access_token"]))
        stored = store_person_sign_in(
            session.owner_canonical_user_id, sign_in_record(client, token, username, now=time.time())
        )
        if stored.status != "success":
            raise XSignInError(
                f"X accepted the sign-in, but the credential store did not save it. {stored.error or ''}".strip(),
                reason="x_sign_in_not_stored",
            )
        return username

    @staticmethod
    def _fail(session: Any, logger: logging.Logger, exc: XSignInError) -> None:
        for key in ("oauth_state", "code_verifier", "client"):
            session.state.pop(key, None)
        session.state.update({"status": STATUS_ERROR, "error": str(exc), "failure_reason": exc.reason})
        logger.warning(
            "X sign-in failed. event=x_sign_in_failed person_id=%s reason=%s",
            session.owner_canonical_user_id,
            exc.reason,
        )

    def render(self, session: Any) -> str:
        status = str(session.state.get("status") or STATUS_CREATED)
        if status == STATUS_SUCCESS:
            return _page(
                "You are signed in to X.",
                f"<p>Agents now use your X account @{html.escape(str(session.state.get('username') or ''))} "
                "only for work they do for you. You can close this page.</p>" + render_close_page_button(),
            )
        if status != STATUS_PENDING:
            message = str(session.state.get("error") or "The X sign-in could not start.")
            return _page("X sign-in did not finish.", render_markdown_text(message) + f"<p>{html.escape(RESTART_TEXT)}</p>")
        return self._render_pending(session)

    def _render_pending(self, session: Any) -> str:

        error = str(session.state.get("error") or "")
        message = error or str(session.state.get("message") or "")
        url = html.escape(str(session.state.get("external_url") or ""), quote=True)
        body = (
            "<p>You sign in with your own X account. Agents use it only for work they do for you, "
            "and the broker keeps it in the credential store.</p>"
            '<ol class="steps">'
            '<li class="step">Select Open and approve the sign-in on X.</li>'
            '<li class="step">X then sends your browser to the team app\'s callback address. If that is this '
            "broker's page, the sign-in finishes by itself.</li>"
            '<li class="step">If the address does not load (for example <code>http://localhost:8080/callback</code>), '
            "copy the full address from the address bar and paste it below.</li></ol>"
            f"{render_session_countdown(session.expires_at)}"
            f'<p><button type="button" class="auth-open-button" data-broker-auth-open data-auth-url="{url}" '
            'aria-label="Open X sign-in in a new tab">Open</button></p>'
            f'{auth_link_html(str(session.state.get("external_url") or ""))}'
            '<div class="status ua-markdown" id="broker-auth-status" role="status" data-auth-provider="x" '
            f'data-restart-command="{html.escape(SIGN_IN_COMMAND, quote=True)}" '
            f'data-markdown="{html.escape(message, quote=True)}"></div>'
            '<form method="post"><p><label for="redirect_url">Address X sent your browser to</label><br>'
            '<textarea id="redirect_url" name="redirect_url" required rows="4" autocomplete="off"></textarea></p>'
            '<button type="submit">Finish sign-in</button></form>'
        )
        return _page("Sign in to X", body, script=sign_in_wait_script())

    def status_payload(self, session: Any) -> dict[str, Any]:

        return session_status_payload(session)

    def cleanup(self, session: Any) -> None:
        for key in ("oauth_state", "code_verifier", "client"):
            session.state.pop(key, None)


class XTeamClientFlow:
    """``/auth x team_client``: an administrator stores the team's X app."""

    provider = X_SIGN_IN_PROVIDER
    method = TEAM_CLIENT_METHOD
    display_name = "X"
    kind = "form"

    def start(self, session: Any, payload: Mapping[str, Any], logger: logging.Logger) -> None:
        return

    def submit(self, session: Any, form: Mapping[str, Any], logger: logging.Logger) -> None:
        # Checked when the session was made and again now, from the signed session.
        if not is_administrator(session.owner_roles):
            logger.warning(
                "Team X app refused at submission. event=team_credential_admin_required "
                "action=x_team_client_submit person_id=%s",
                session.owner_canonical_user_id,
            )
            session.state.update({"status": STATUS_ERROR, "error": TEAM_CLIENT_ADMIN_MESSAGE})
            return
        client = XClient(
            client_id=str(form.get("client_id") or "").strip(),
            client_secret=str(form.get("client_secret") or "").strip(),
            redirect_uri=str(form.get("redirect_uri") or "").strip(),
        )
        result = store_team_client(
            str(session.owner_team_id or ""), client, stored_by=session.owner_canonical_user_id
        )
        if result.status != "success":
            session.state["error"] = result.error or "The credential store did not save the X app."
            return
        session.state.pop("error", None)
        session.state["status"] = STATUS_SUCCESS

    def render(self, session: Any) -> str:
        status = str(session.state.get("status") or STATUS_CREATED)
        if status == STATUS_SUCCESS:
            return _page(
                "The team's X sign-in app is stored.",
                f"<p>People sign in with their own X accounts with <code>{SIGN_IN_COMMAND}</code>. "
                "Sign-ins made through a different app must be made again.</p>" + render_close_page_button(),
            )
        if status == STATUS_ERROR:
            return _page("The X sign-in app was not stored.", render_markdown_text(str(session.state.get("error") or "")))
        return _page("Set the team's X sign-in app.", self._form_body(session), script=SECRET_INPUT_TOGGLE_SCRIPT + _CALLBACK_PREFILL_SCRIPT)

    @staticmethod
    def _form_body(session: Any) -> str:
        error = str(session.state.get("error") or "")
        return (
            "<p>People sign in to X with their own accounts through this app. Storing it gives nobody access "
            "to anyone's X account.</p>"
            '<ol class="steps">'
            '<li class="step">In the X developer portal, open your app&#39;s <strong>User authentication settings</strong> '
            "and turn on OAuth 2.0 with <strong>Read and write</strong> permissions.</li>"
            '<li class="step">Choose <strong>Native App</strong> (no client secret) or <strong>Web App</strong> '
            "(X shows a client secret once).</li>"
            '<li class="step">Add a callback address. The suggested one below is this broker\'s own page, so '
            "sign-ins finish by themselves; <code>http://localhost:8080/callback</code> also works, and people "
            "then paste the address their browser shows.</li>"
            '<li class="step">Copy the OAuth 2.0 client ID (and secret) from <strong>Keys and tokens</strong>.</li></ol>'
            f"{render_markdown_text(error, class_name='status') if error else ''}"
            f"{render_session_countdown(session.expires_at)}"
            '<form method="post">'
            '<div><label for="client_id">OAuth 2.0 client ID</label>'
            '<input id="client_id" name="client_id" type="text" autocomplete="off" required></div>'
            '<div><label for="client_secret">Client secret (Web App only; leave empty for a Native App)</label>'
            + render_credential_input(input_id="client_secret", input_type="password", autocomplete="new-password")
            + "</div>"
            '<div><label for="redirect_uri">Callback address registered in the X app</label>'
            f'<input id="redirect_uri" name="redirect_uri" type="url" autocomplete="off" required '
            f'data-callback-path="{CALLBACK_PATH}"></div>'
            "<button type=\"submit\">Store the team's app</button></form>"
        )

    def status_payload(self, session: Any) -> dict[str, Any]:

        return session_status_payload(session)

    def cleanup(self, session: Any) -> None:
        return


# Suggest the broker's own callback page, as the browser reaches it.
_CALLBACK_PREFILL_SCRIPT = """
<script>
(() => {
  const input = document.getElementById("redirect_uri");
  if (input && !input.value) input.value = window.location.origin + input.getAttribute("data-callback-path");
})();
</script>"""


__all__ = [
    "CALLBACK_PATH",
    "CLIENT_MISSING_MESSAGE",
    "SIGN_IN_REQUIRED_MESSAGE",
    "X_CLIENT_KEY",
    "X_SIGN_IN_KEY",
    "X_SIGN_IN_SESSION_KEY",
    "XClient",
    "XSignInError",
    "XTeamClientFlow",
    "XWebSignInFlow",
    "person_sign_in",
    "store_team_client",
    "team_client",
    "x_auth_status",
]
