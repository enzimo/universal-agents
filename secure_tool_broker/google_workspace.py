"""Google Workspace (``gog``) for one person at a time.

Why this module exists: ``gog`` keeps its OAuth client, its signed-in accounts
and its keyring under one home folder (``GOG_HOME``, else ``$HOME/.config``).
The broker used to run every person's ``gog`` in the broker's own home, so all
people shared one keyring and a call could use whichever Google account was
last imported. Now:

* every ``gog`` run gets a fresh home in a private folder under the person's
  broker credentials folder (``users/<owner>/gog/runs/``), with the file
  keyring protected by ``GOG_KEYRING_PASSWORD``, and the home is removed when
  the run ends, so nothing from one run or one person is left for another;
* ``gog`` gets an allowlisted environment, never the broker's: ``gog`` reads
  ``GOG_ACCESS_TOKEN``, API keys and other secrets from its environment, and
  the broker holds other tools' credentials;
* the only Google account in that home is the person's own sign-in, read from
  the credential store (``gog.token_json``); a person with no sign-in is
  refused with a plain next step and ``gog`` never starts;
* the OAuth client (the Google Cloud "Desktop app" that people sign in
  through) is the person's own when they stored one, else the team's, which an
  administrator stores once with ``/auth google team_client``; a person may
  paste their own on the sign-in page (``/link cli:google``). People still
  sign in with their own Google accounts; the client only identifies the app.

Stored state is the source of truth; each run installs the client and imports
the token again (the broker did both on every call before as well).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

from secure_tool_broker.auth_models import AuthStatus
from secure_tool_broker.credential_state import (
    broker_credential_file,
    credential_owner_context,
)
from secure_tool_broker.credential_state_store import (
    read_credential_state,
    write_credential_state,
)
from secure_tool_broker.redaction import redact_text
from secure_tools.auth_methods import SIGN_IN_TOOLS, team_client_command
from secure_tools.providers import ToolResult

LOGGER = logging.getLogger(__name__)

GOOGLE_PROVIDER = "google"
GOOGLE_TOKEN_KEY = "gog.token_json"
GOOGLE_CLIENT_KEY = "gog.oauth_client_credentials_json"
GOG_KEYRING_PASSWORD_ENV = "GOG_KEYRING_PASSWORD"
GOG_EXECUTABLE = "gog"

SIGN_IN_COMMAND = SIGN_IN_TOOLS["cli:google"].command
TOKEN_IMPORT_COMMAND = f"{SIGN_IN_COMMAND} json_token"
TEAM_CLIENT_COMMAND = team_client_command(GOOGLE_PROVIDER)

CLIENT_OWNER_PERSON = "person"
CLIENT_OWNER_TEAM = "team"

ERROR_SIGN_IN_REQUIRED = "auth_required"
ERROR_CLIENT_MISSING = "google_oauth_client_missing"
ERROR_KEYRING_PASSWORD_MISSING = "google_keyring_password_missing"
ERROR_UNAUTHORIZED_CLIENT = "google_unauthorized_client"
ERROR_REFRESH_TOKEN_INVALID = "google_refresh_token_invalid"
ERROR_PERSON_REQUIRED = "google_person_required"

# Copied from the broker's environment into gog's: locale, time zone, CA
# certificates and proxies, which gog needs to reach Google from the broker's
# network. Nothing else crosses (GOG_ACCESS_TOKEN, API keys, other tokens).
_PASSED_ENV = (
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
    "https_proxy",
    "http_proxy",
    "no_proxy",
)
_GOG_TIMEOUT_SECONDS = 15
_GOG_CHECK_TIMEOUT_SECONDS = 20
# /auth status checks every provider within the gateway's 10-second request, so
# the Google check there gets a shorter limit (gog's own --timeout, then ours).
STATUS_CHECK_TIMEOUT_SECONDS = 6

KEYRING_PASSWORD_MESSAGE = (
    "The broker cannot open Google's keyring because GOG_KEYRING_PASSWORD is not set for the secure "
    "tool broker. An operator sets GOG_KEYRING_PASSWORD (or GOG_KEYRING_PASSWORD_FILE) in the broker's "
    f"environment and restarts it; then run {SIGN_IN_COMMAND} again."
)
CLIENT_MISSING_MESSAGE = (
    "No Google sign-in app (OAuth client) is set up yet. A team administrator stores the team's app once "
    f"with {TEAM_CLIENT_COMMAND}, or you paste your own on the sign-in page ({SIGN_IN_COMMAND})."
)
SIGN_IN_REQUIRED_MESSAGE = (
    f"You have not signed in to Google for the agents yet. Sign in with {SIGN_IN_COMMAND}; "
    "the broker then uses your own Google account for your requests only."
)
PERSON_REQUIRED_MESSAGE = (
    "Google tools run only for a signed-in person, and this request names none."
)
UNAUTHORIZED_CLIENT_MESSAGE = (
    "Google refused your sign-in because it was made through a different Google sign-in app "
    f"(OAuth client) than the one set up now. Sign in again with {SIGN_IN_COMMAND} so the token matches."
)
REFRESH_TOKEN_INVALID_MESSAGE = (
    "Google rejected your stored sign-in because it expired or was revoked. Sign in again with "
    f"{SIGN_IN_COMMAND}. If it stopped working about 7 days after you signed in, the team's Google Cloud "
    "OAuth consent screen is probably still in Testing; an administrator changes its publishing "
    "status to In Production first."
)


class GoogleNotReady(Exception):
    """gog cannot run for this person; ``result`` says why and what to do."""

    def __init__(self, result: ToolResult) -> None:
        super().__init__(result.error or "Google is not ready.")
        self.result = result


@dataclass(frozen=True)
class OAuthClient:
    """The OAuth client JSON a run uses, and whose it is (the person's or the team's)."""

    json_text: str
    owner: str


def not_ready(
    error_code: str, message: str, *, content: Mapping[str, Any] | None = None, auth_required: bool = True
) -> ToolResult:
    """A refusal that names the next step (``user_action``) in plain language."""

    body = dict(content or {})
    body.update(
        {
            "error_code": error_code,
            "provider": GOOGLE_PROVIDER,
            "auth_required": auth_required,
            "auth_message": message,
            "user_action": SIGN_IN_COMMAND,
            "auth_options": [SIGN_IN_COMMAND, TOKEN_IMPORT_COMMAND],
        }
    )
    return ToolResult(status="error", content=body, error=message)


# -- The OAuth client --------------------------------------------------------------


def desktop_client_problem(text: str) -> str | None:
    """Why ``text`` is not a Google Cloud "Desktop app" OAuth client JSON, or None."""

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        return f"The OAuth client must be the JSON file downloaded from Google Cloud Console ({exc.msg})."
    if not isinstance(parsed, dict):
        return "The OAuth client must be the JSON object downloaded from Google Cloud Console."
    if isinstance(parsed.get("web"), Mapping):
        return (
            "This is a Web application OAuth client. gog signs in through a local redirect address that "
            "changes each time, which only a Desktop app client accepts; a Web client fails with "
            "Error 400: redirect_uri_mismatch. Create an OAuth client with Application type Desktop app "
            "and download its JSON."
        )
    if not isinstance(parsed.get("installed"), Mapping):
        return (
            "The OAuth client JSON must be a Desktop app client. In Google Cloud Console choose "
            "Create Credentials > OAuth client ID > Application type Desktop app, then download its JSON."
        )
    return None


def _read_state(key: str) -> str | None:
    """A stored value for the active owner; a store failure stops the run."""

    value = read_credential_state(key)
    if isinstance(value, ToolResult):
        raise GoogleNotReady(value)
    return str(value).strip() or None if value else None


def oauth_client_for(person_id: str, team_id: str | None) -> OAuthClient | None:
    """The person's own OAuth client, else the team's, else None."""

    with credential_owner_context(person_id):
        own = _read_state(GOOGLE_CLIENT_KEY)
    if own:
        return OAuthClient(own, CLIENT_OWNER_PERSON)
    if not team_id:
        return None
    with credential_owner_context(team_id, scope_kind="team"):
        team = _read_state(GOOGLE_CLIENT_KEY)
    return OAuthClient(team, CLIENT_OWNER_TEAM) if team else None


def person_token(person_id: str) -> str | None:
    with credential_owner_context(person_id):
        return _read_state(GOOGLE_TOKEN_KEY)


def store_person_client(person_id: str, client_json: str) -> ToolResult:
    with credential_owner_context(person_id):
        return write_credential_state(GOOGLE_CLIENT_KEY, client_json)


def store_person_token(person_id: str, token_json: str) -> ToolResult:
    with credential_owner_context(person_id):
        return write_credential_state(GOOGLE_TOKEN_KEY, token_json)


def store_team_client(team_id: str, client_json: str, *, stored_by: str) -> ToolResult:
    """Store the team's OAuth client; the caller has checked administrator authority."""

    text = str(client_json or "").strip()
    problem = desktop_client_problem(text) if text else "Paste the OAuth client JSON."
    if problem:
        return ToolResult(status="error", error=problem, content={"error_code": "google_oauth_client_invalid"})
    with credential_owner_context(team_id, scope_kind="team"):
        result = write_credential_state(GOOGLE_CLIENT_KEY, text)
    if result.status == "success":
        LOGGER.info(
            "The team's Google OAuth client was stored. event=google_team_client_stored team_id=%s person_id=%s",
            team_id,
            stored_by,
        )
    else:
        LOGGER.warning(
            "The team's Google OAuth client could not be stored. event=google_team_client_store_failed "
            "team_id=%s person_id=%s error=%s",
            team_id,
            stored_by,
            redact_text(str(result.error or "")),
        )
    return result


# -- A private gog home --------------------------------------------------------------


def _runs_folder(person_id: str) -> Path:
    with credential_owner_context(person_id):
        anchor = broker_credential_file("gog", "runs", "anchor")
    if anchor is None:
        raise GoogleNotReady(
            ToolResult(
                status="error",
                error="The secure tool broker has no credentials folder (SECURE_TOOL_BROKER_CREDENTIALS_DIR).",
                content={"error_code": "broker_credentials_directory_required", "provider": GOOGLE_PROVIDER},
            )
        )
    return anchor.parent


def gog_environment(home: Path) -> dict[str, str]:
    """gog's whole environment for one private home (see ``_PASSED_ENV``)."""

    password = str(os.environ.get(GOG_KEYRING_PASSWORD_ENV) or "")
    if not password:
        raise GoogleNotReady(not_ready(ERROR_KEYRING_PASSWORD_MISSING, KEYRING_PASSWORD_MESSAGE, auth_required=False))
    env = {key: os.environ[key] for key in _PASSED_ENV if key in os.environ}
    env.update(
        {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / "config"),
            "GOG_HOME": str(home / "gogcli"),
            "GOG_KEYRING_BACKEND": "file",
            GOG_KEYRING_PASSWORD_ENV: password,
            "NO_COLOR": "1",
        }
    )
    return env


def make_gog_home(person_id: str) -> Path:
    """A new empty home for one run or one sign-in; ``remove_gog_home`` deletes it."""

    return Path(tempfile.mkdtemp(prefix="run-", dir=_runs_folder(person_id)))


def remove_gog_home(home: Path | str | None) -> None:
    if home:
        shutil.rmtree(home, ignore_errors=True)


@contextmanager
def private_gog_home(person_id: str) -> Iterator[dict[str, str]]:
    """gog's environment for one run in a fresh home, removed afterwards."""

    home = make_gog_home(person_id)
    try:
        yield gog_environment(home)
    finally:
        remove_gog_home(home)


# -- Running gog -----------------------------------------------------------------------


def run_gog(
    command: list[str], env: Mapping[str, str], *, stdin: str | None = None, timeout: int = _GOG_TIMEOUT_SECONDS
) -> subprocess.CompletedProcess[str] | ToolResult:
    """Run gog in its home with the given environment only; secrets go by stdin."""

    input_kwargs: dict[str, Any] = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
    try:
        return subprocess.run(
            command,
            cwd=env["HOME"],
            env=dict(env),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            **input_kwargs,
        )
    except subprocess.TimeoutExpired:
        return ToolResult(status="error", error=f"gog timed out after {timeout}s.")
    except OSError as exc:
        return ToolResult(status="error", error=f"gog could not start: {redact_text(str(exc))}")


def classify_failure(content: Mapping[str, Any], *, output: str) -> ToolResult | None:
    """A plain refusal for a gog failure Google explained, or None."""

    lowered = output.lower()
    if "unauthorized_client" in lowered:
        return not_ready(ERROR_UNAUTHORIZED_CLIENT, UNAUTHORIZED_CLIENT_MESSAGE, content=content)
    if "invalid_grant" in lowered or "token has been expired or revoked" in lowered:
        return not_ready(ERROR_REFRESH_TOKEN_INVALID, REFRESH_TOKEN_INVALID_MESSAGE, content=content)
    if "gog_keyring_password" in lowered or ("no tty" in lowered and "keyring" in lowered):
        return not_ready(ERROR_KEYRING_PASSWORD_MISSING, KEYRING_PASSWORD_MESSAGE, content=content, auth_required=False)
    return None


def _setup_step(executable: str, env: Mapping[str, str], args: list[str], secret: str, action: str) -> None:
    """One setup command fed a secret on stdin; raises ``GoogleNotReady`` on failure."""

    completed = run_gog([executable, *args], env, stdin=secret if secret.endswith("\n") else f"{secret}\n")
    if isinstance(completed, ToolResult):
        raise GoogleNotReady(completed)
    if completed.returncode == 0:
        return
    stderr = redact_text(completed.stderr or "").strip()
    content = {"provider": GOOGLE_PROVIDER, "action": action, "exit_code": completed.returncode, "stderr": stderr}
    known = classify_failure(content, output=f"{completed.stdout or ''}\n{completed.stderr or ''}")
    raise GoogleNotReady(
        known or ToolResult(status="error", content=content, error=f"gog {' '.join(args[:3])} exited with code {completed.returncode}.")
    )


def install_client(executable: str, env: Mapping[str, str], client: OAuthClient) -> None:
    _setup_step(executable, env, ["auth", "credentials", "set", "-"], client.json_text, "auth.credentials.set")


def import_token(executable: str, env: Mapping[str, str], token_json: str) -> None:
    _setup_step(executable, env, ["auth", "tokens", "import", "-"], token_json, "auth.tokens.import")


def require_client(person_id: str, team_id: str | None) -> OAuthClient:
    client = oauth_client_for(person_id, team_id)
    if client is None:
        raise GoogleNotReady(not_ready(ERROR_CLIENT_MISSING, CLIENT_MISSING_MESSAGE))
    return client


def require_person(person_id: str | None) -> str:
    rendered = str(person_id or "").strip()
    if not rendered:
        raise GoogleNotReady(not_ready(ERROR_PERSON_REQUIRED, PERSON_REQUIRED_MESSAGE, auth_required=False))
    return rendered


@contextmanager
def signed_in_google(executable: str, *, person_id: str | None, team_id: str | None) -> Iterator[dict[str, str]]:
    """gog's environment with only this person's sign-in installed.

    Raises ``GoogleNotReady`` before gog runs when the person has no stored
    sign-in or no OAuth client is set up.
    """

    person = require_person(person_id)
    token = person_token(person)
    if not token:
        LOGGER.info("Google refused: no stored sign-in. event=google_sign_in_missing person_id=%s", person)
        raise GoogleNotReady(not_ready(ERROR_SIGN_IN_REQUIRED, SIGN_IN_REQUIRED_MESSAGE))
    client = require_client(person, team_id)
    with private_gog_home(person) as env:
        install_client(executable, env, client)
        import_token(executable, env, token)
        yield env


# -- Checking a sign-in ---------------------------------------------------------------


def verified_google_account(stdout: str) -> tuple[bool, str]:
    """Whether ``gog auth list --check`` reported a valid account, and why not."""

    try:
        parsed = json.loads(stdout or "{}")
    except json.JSONDecodeError:
        return False, "gog auth list --check returned non-JSON output."
    accounts = parsed.get("accounts") if isinstance(parsed, dict) else None
    if not isinstance(accounts, list) or not accounts:
        return False, "gog auth list --check did not report any Google accounts."
    errors: list[str] = []
    for account in accounts:
        if not isinstance(account, dict):
            continue
        if account.get("valid") is True:
            return True, str(account.get("email") or "")
        errors.append(f"{account.get('email') or 'unknown account'}: {account.get('error') or 'not marked valid'}")
    return False, "; ".join(errors) or "No Google account is marked valid."


def check_sign_in(
    executable: str, env: Mapping[str, str], *, timeout_seconds: int = _GOG_CHECK_TIMEOUT_SECONDS
) -> tuple[bool, str]:
    """Ask Google whether the sign-in in this home works (a token exchange)."""

    completed = run_gog(
        [executable, "--json", "--no-input", "auth", "list", "--check", f"--timeout={max(1, timeout_seconds - 1)}s"],
        env,
        timeout=timeout_seconds,
    )
    if isinstance(completed, ToolResult):
        return False, str(completed.error or "gog auth list --check failed.")
    if completed.returncode != 0:
        output = redact_text(f"{completed.stdout or ''}\n{completed.stderr or ''}").strip()
        return False, output or f"gog auth list --check exited with code {completed.returncode}."
    return verified_google_account(completed.stdout or "")


# -- Status and sign-in by token import ----------------------------------------------

_STATUS_OPTIONS = ("web", "json_token", "credentials", "team_client")


def _status(status: str, message: str, *, details: Mapping[str, Any] | None = None) -> AuthStatus:
    return AuthStatus(
        provider=GOOGLE_PROVIDER,
        status=status,
        authenticated=status == "authenticated",
        message=message,
        auth_options=_STATUS_OPTIONS,
        details=details,
    )


def google_auth_status(person_id: str | None, team_id: str | None) -> AuthStatus:
    """The person's Google status, checked with Google itself.

    "authenticated" only after ``gog auth list --check`` exchanged the stored
    refresh token for an access token; a stored token that Google refuses is
    reported as needing a new sign-in, never as configured.
    """

    executable = shutil.which(GOG_EXECUTABLE)
    if not executable:
        return _status("unavailable", "The Google Workspace CLI (gog) is not installed in the secure tool broker.")
    try:
        with signed_in_google(executable, person_id=person_id, team_id=team_id) as env:
            valid, detail = check_sign_in(executable, env, timeout_seconds=STATUS_CHECK_TIMEOUT_SECONDS)
    except GoogleNotReady as exc:
        code = str(exc.result.content.get("error_code") or "")
        status = "auth_required" if exc.result.content.get("auth_required") else "unavailable"
        return _status(status, str(exc.result.error), details={"error_code": code} if code else None)
    if valid:
        return _status(
            "authenticated",
            f"Signed in to Google as {detail}; Google accepted the stored sign-in just now.",
            details={"account": detail} if detail else None,
        )
    LOGGER.info(
        "Google refused a stored sign-in during the status check. event=google_sign_in_check_failed person_id=%s",
        person_id,
    )
    return _status(
        "auth_required",
        f"Your stored Google sign-in no longer works (Google refused it). Sign in again with {SIGN_IN_COMMAND}.",
        details={"check": redact_text(detail)[:500]},
    )


def login_google_with_token_json(
    token_json: str, *, person_id: str | None, team_id: str | None, credentials_json: str | None = None
) -> ToolResult:
    """Store a person's exported gog token after Google accepts it.

    An OAuth client pasted with it is checked and stored as the person's own
    client; otherwise the person's stored client or the team's is used.
    """

    token_text = str(token_json or "").strip()
    client_text = str(credentials_json or "").strip()
    try:
        person = require_person(person_id)
        _check_token_payload(token_text, client_text)
        executable = _require_gog()
        client = OAuthClient(client_text, CLIENT_OWNER_PERSON) if client_text else require_client(person, team_id)
        with private_gog_home(person) as env:
            install_client(executable, env, client)
            import_token(executable, env, token_text)
            valid, detail = check_sign_in(executable, env)
    except GoogleNotReady as exc:
        return exc.result
    if not valid:
        return ToolResult(
            status="error",
            error=f"Google did not accept this token: {redact_text(detail)}",
            content={"provider": GOOGLE_PROVIDER, "action": "auth.tokens.import", "error_code": "google_token_rejected"},
        )
    return _store_sign_in(person, token_text, client_text, account=detail)


def _check_token_payload(token_text: str, client_text: str) -> None:
    if not token_text:
        raise GoogleNotReady(ToolResult(status="error", error="Paste the gog token JSON."))
    try:
        json.loads(token_text)
    except json.JSONDecodeError as exc:
        raise GoogleNotReady(ToolResult(status="error", error=f"The gog token must be JSON ({exc.msg}).")) from None
    problem = desktop_client_problem(client_text) if client_text else None
    if problem:
        raise GoogleNotReady(ToolResult(status="error", error=problem))


def _require_gog() -> str:
    executable = shutil.which(GOG_EXECUTABLE)
    if not executable:
        raise GoogleNotReady(ToolResult(status="error", error="The Google Workspace CLI (gog) is not installed."))
    return executable


def _store_sign_in(person: str, token_text: str, client_text: str, *, account: str) -> ToolResult:
    if client_text:
        stored_client = store_person_client(person, client_text)
        if stored_client.status != "success":
            return stored_client
    stored = store_person_token(person, token_text)
    if stored.status != "success":
        return stored
    LOGGER.info(
        "A person's Google sign-in was stored. event=google_sign_in_stored person_id=%s own_client=%s",
        person,
        bool(client_text),
    )
    return ToolResult(
        status="success",
        content={
            "provider": GOOGLE_PROVIDER,
            "action": "auth.tokens.import",
            "account": account,
            "credential_state_persisted": True,
            "own_oauth_client_stored": bool(client_text),
        },
    )


__all__ = [
    "CLIENT_MISSING_MESSAGE",
    "GOOGLE_CLIENT_KEY",
    "GOOGLE_TOKEN_KEY",
    "GoogleNotReady",
    "KEYRING_PASSWORD_MESSAGE",
    "OAuthClient",
    "TEAM_CLIENT_COMMAND",
    "check_sign_in",
    "classify_failure",
    "desktop_client_problem",
    "gog_environment",
    "google_auth_status",
    "install_client",
    "login_google_with_token_json",
    "make_gog_home",
    "oauth_client_for",
    "remove_gog_home",
    "require_client",
    "signed_in_google",
    "store_person_token",
    "store_team_client",
    "verified_google_account",
]
